import subprocess
import sys

import pytest

from mcp_server.git_integration import GitIntegrator
from mcp_server import integration_worker
from mcp_server.supervisor import SupervisorRuntime


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def make_repo(tmp_path, conflict=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "shared.txt")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    sources = {}
    for name in ("claude", "codex"):
        git(repo, "checkout", "-b", name, base)
        if conflict:
            (repo / "shared.txt").write_text(name + "\n", encoding="utf-8")
        else:
            (repo / f"{name}.txt").write_text(name + "\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", name)
        sources[name] = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    return repo, base, sources


def make_participant_worktrees(tmp_path, repo, source_shas, monkeypatch):
    root = tmp_path / "participant-worktrees"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(root))
    for participant, source_sha in source_shas.items():
        path = root / f"i1-{participant}"
        branch = f"a2a/issue-i1/{participant}"
        git(repo, "worktree", "add", "-b", branch, str(path), source_sha)


def test_merge_builds_third_candidate_from_exact_source_commits_and_verifies_it(tmp_path):
    repo, base, sources = make_repo(tmp_path)
    runtime = SupervisorRuntime(tmp_path / "supervisor")
    integrator = GitIntegrator(tmp_path / "home", runtime)

    merged = integrator.merge_candidate(str(repo), "i1", "g1", base, sources)
    candidate = merged["candidate_sha"]

    assert merged["status"] == "merged"
    assert candidate not in {*sources.values(), base}
    for source_sha in sources.values():
        git(repo, "merge-base", "--is-ancestor", source_sha, candidate)
    result = integrator.verify(merged["worktree"], [sys.executable, "-c", "print('verified')"])
    assert result["status"] == "pass"
    assert result["stdout"].strip() == "verified"
    failed = integrator.verify(merged["worktree"], [sys.executable, "-c", "raise SystemExit(7)"])
    assert failed["status"] == "fail"
    assert failed["exit_code"] == 7


def test_merge_conflict_is_reported_without_moving_base_ref(tmp_path):
    repo, base, sources = make_repo(tmp_path, conflict=True)
    integrator = GitIntegrator(tmp_path / "home", SupervisorRuntime(tmp_path / "supervisor"))

    result = integrator.merge_candidate(str(repo), "i1", "g1", base, sources)

    assert result["status"] == "conflict"
    assert "conflict" in result["detail"]
    assert git(repo, "rev-parse", "refs/heads/main") == base


def test_update_ref_uses_compare_and_swap_and_rejects_stale_base(tmp_path):
    repo, base, sources = make_repo(tmp_path)
    integrator = GitIntegrator(tmp_path / "home", SupervisorRuntime(tmp_path / "supervisor"))
    merged = integrator.merge_candidate(str(repo), "i1", "g1", base, sources)
    candidate = merged["candidate_sha"]
    integrator.advance_ref(str(repo), "refs/heads/main", candidate, base)
    assert git(repo, "rev-parse", "refs/heads/main") == candidate

    with pytest.raises(RuntimeError, match="compare-and-swap"):
        integrator.advance_ref(str(repo), "refs/heads/main", base, base)
    assert git(repo, "rev-parse", "refs/heads/main") == candidate


@pytest.mark.parametrize("test_command, expected_state", [
    ([sys.executable, "-c", "print('ok')"], "advanced"),
    ([sys.executable, "-c", "raise SystemExit(5)"], "failed"),
])
def test_integration_worker_posts_stages_and_moves_ref_only_after_exact_tests(
        tmp_path, monkeypatch, test_command, expected_state):
    repo, base, sources = make_repo(tmp_path)
    make_participant_worktrees(tmp_path, repo, sources, monkeypatch)

    class FakeService:
        def __init__(self, _url):
            self.result = None

        def integration_heartbeat(self, *_args, **_kwargs):
            return {}

        def integration_candidate(self, issue_id, integration_id, **body):
            self.candidate = body
            return {}

        def integration_tests(self, issue_id, integration_id, **body):
            self.tests = body
            return {}

        def snapshot(self, issue_id):
            return {"issue": {"version": 7}}

        def advance_integration(self, issue_id, integration_id, **body):
            self.advanced = body
            return {}

        def integration_result(self, issue_id, integration_id, **body):
            self.result = body
            return {}

    instances = []

    def make_service(url):
        service = FakeService(url)
        instances.append(service)
        return service

    monkeypatch.setattr(integration_worker, "IssueService", make_service)
    config = {
        "balancer_url": "http://test", "supervisor_dir": str(tmp_path / "supervisor"),
        "a2a_link_home": str(tmp_path / "home"), "repo": str(repo),
        "integration_id": "g1", "issue_id": "i1", "owner": "supervisor:test:install",
        "expected_base_sha": base, "source_shas": sources,
        "verification_argv": test_command,
    }

    exit_code = integration_worker.run(config)
    service = instances[0]

    assert exit_code == 0
    assert service.candidate["status"] == "merged"
    assert service.tests["argv"] == test_command
    if expected_state == "advanced":
        assert service.advanced["expected_version"] == 7
        assert service.result["status"] == "advanced"
        assert git(repo, "rev-parse", "refs/heads/main") == service.result["new_base_sha"]
        for participant in sources:
            path = tmp_path / "participant-worktrees" / f"i1-{participant}"
            assert git(path, "rev-parse", "HEAD") == service.result["new_base_sha"]
            assert git(path, "branch", "--show-current") == f"a2a/issue-i1/{participant}"
            assert git(path, "status", "--porcelain", "--untracked-files=all") == ""
    else:
        assert service.result is None
        assert git(repo, "rev-parse", "refs/heads/main") == base


@pytest.mark.parametrize("stage", ["tested", "advancing", "ref_done"])
def test_integration_worker_recovery_does_not_repeat_completed_stages(tmp_path, monkeypatch, stage):
    repo, base, sources = make_repo(tmp_path)
    make_participant_worktrees(tmp_path, repo, sources, monkeypatch)
    home = tmp_path / "home"
    supervisor_dir = tmp_path / "supervisor"
    runtime = SupervisorRuntime(supervisor_dir)
    assert runtime.acquire()
    integrator = GitIntegrator(home, runtime)
    merged = integrator.merge_candidate(str(repo), "i1", "g1", base, sources)
    candidate, ref = merged["candidate_sha"], merged["ref"]
    journal = runtime.journal
    journal.append("merge_done", integration_id="g1", candidate_sha=candidate,
                   output=merged["output"], status="merged", worktree=merged["worktree"], ref=ref)
    journal.append("tests_done", integration_id="g1", candidate_sha=candidate,
                   argv=[sys.executable, "-c", "print('verified')"], status="pass",
                   exit_code=0, stdout="verified\n", stderr="", duration_s=0.01, truncated=False)
    if stage in ("advancing", "ref_done"):
        journal.append("ref_update_intent", integration_id="g1", expected_base_sha=base,
                       candidate_sha=candidate, ref=ref)
        # Simulate the crash after git update-ref and before SQLite result, or a ref_update_done replay.
        git(repo, "update-ref", ref, candidate, base)
    if stage == "ref_done":
        journal.append("ref_update_done", integration_id="g1", candidate_sha=candidate,
                       ref=ref, output={"exit_code": 0, "stdout": "", "stderr": "", "truncated": False})
    runtime.release()

    class FakeService:
        def __init__(self, _url):
            self.result = None
            self.advance_count = 0
            self.candidate_count = 0
            self.tests_count = 0

        def integration_heartbeat(self, *_args, **_kwargs): return {}
        def integration_candidate(self, *_args, **_kwargs): self.candidate_count += 1; return {}
        def integration_tests(self, *_args, **_kwargs): self.tests_count += 1; return {}
        def snapshot(self, _issue_id): return {"issue": {"version": 12}}
        def advance_integration(self, *_args, **_kwargs): self.advance_count += 1; return {}
        def integration_result(self, _issue_id, _integration_id, **body): self.result = body; return {}

    services = []
    monkeypatch.setattr(integration_worker, "IssueService", lambda url: (services.append(FakeService(url)) or services[-1]))
    monkeypatch.setattr(GitIntegrator, "merge_candidate", lambda *_a, **_k: pytest.fail("completed merge was repeated"))
    monkeypatch.setattr(GitIntegrator, "verify", lambda *_a, **_k: pytest.fail("completed tests were repeated"))
    if stage != "tested":
        monkeypatch.setattr(GitIntegrator, "advance_ref", lambda *_a, **_k: pytest.fail("ref update was repeated"))
    config = {"balancer_url": "http://test", "supervisor_dir": str(supervisor_dir),
              "a2a_link_home": str(home), "repo": str(repo), "integration_id": "g1",
              "issue_id": "i1", "owner": "supervisor:test:install", "expected_base_sha": base,
              "source_shas": sources, "verification_argv": [sys.executable, "-c", "print('verified')"]}

    assert integration_worker.run(config) == 0
    service = services[0]
    assert service.candidate_count == 1 and service.tests_count == 1
    assert service.result["status"] == "advanced"
    assert service.result["new_base_sha"] == candidate
    assert git(repo, "rev-parse", ref) == candidate
    assert service.advance_count == (1 if stage == "tested" else 0)


def test_participant_worktrees_sync_to_candidate_with_branch_compare_and_swap(tmp_path, monkeypatch):
    repo, base, sources = make_repo(tmp_path)
    make_participant_worktrees(tmp_path, repo, sources, monkeypatch)
    runtime = SupervisorRuntime(tmp_path / "supervisor")
    integrator = GitIntegrator(tmp_path / "home", runtime)
    merged = integrator.merge_candidate(str(repo), "i1", "g-sync", base, sources)
    integrator.preflight_participant_worktrees(str(repo), "i1", sources, merged["candidate_sha"])
    journal = runtime.journal

    synced = integrator.sync_participant_worktrees(str(repo), "i1", "g-sync", sources,
                                                  merged["candidate_sha"], journal)

    assert {row["participant"] for row in synced} == set(sources)
    for participant in sources:
        path = tmp_path / "participant-worktrees" / f"i1-{participant}"
        branch = f"a2a/issue-i1/{participant}"
        assert git(repo, "rev-parse", f"refs/heads/{branch}") == merged["candidate_sha"]
        assert git(path, "rev-parse", "HEAD") == merged["candidate_sha"]
        assert git(path, "status", "--porcelain", "--untracked-files=all") == ""


def test_dirty_participant_worktree_refuses_preflight_without_losing_user_files(tmp_path, monkeypatch):
    repo, base, sources = make_repo(tmp_path)
    make_participant_worktrees(tmp_path, repo, sources, monkeypatch)
    runtime = SupervisorRuntime(tmp_path / "supervisor")
    integrator = GitIntegrator(tmp_path / "home", runtime)
    merged = integrator.merge_candidate(str(repo), "i1", "g-dirty", base, sources)
    claude_path = tmp_path / "participant-worktrees" / "i1-claude"
    user_file = claude_path / "keep-me.txt"
    user_file.write_text("uncommitted user data\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="dirty"):
        integrator.preflight_participant_worktrees(str(repo), "i1", sources, merged["candidate_sha"])

    assert user_file.read_text(encoding="utf-8") == "uncommitted user data\n"
    for participant, source_sha in sources.items():
        branch = f"refs/heads/a2a/issue-i1/{participant}"
        assert git(repo, "rev-parse", branch) == source_sha
    assert git(repo, "rev-parse", "refs/heads/main") == base


def test_worktree_sync_recovers_ref_cas_before_checkout_reset(tmp_path, monkeypatch):
    repo, base, sources = make_repo(tmp_path)
    make_participant_worktrees(tmp_path, repo, sources, monkeypatch)
    runtime = SupervisorRuntime(tmp_path / "supervisor")
    integrator = GitIntegrator(tmp_path / "home", runtime)
    merged = integrator.merge_candidate(str(repo), "i1", "g-recover", base, sources)
    candidate = merged["candidate_sha"]
    # Simulate crash after the branch ref CAS but before this participant worktree's HEAD reset.
    git(repo, "update-ref", "refs/heads/a2a/issue-i1/claude", candidate, sources["claude"])
    runtime.journal.append("worktree_sync_intent", integration_id="g-recover", participant="claude",
                           worktree=str(tmp_path / "participant-worktrees" / "i1-claude"),
                           branch="a2a/issue-i1/claude", expected_source_sha=sources["claude"],
                           candidate_sha=candidate)

    integrator.sync_participant_worktrees(str(repo), "i1", "g-recover", sources,
                                          candidate, runtime.journal)
    integrator.sync_participant_worktrees(str(repo), "i1", "g-recover", sources,
                                          candidate, runtime.journal)

    for participant in sources:
        path = tmp_path / "participant-worktrees" / f"i1-{participant}"
        assert git(path, "rev-parse", "HEAD") == candidate
    events = runtime.journal.read()
    for participant in sources:
        assert sum(row.get("event") == "worktree_sync_done" and row.get("participant") == participant
                   for row in events) == 1
