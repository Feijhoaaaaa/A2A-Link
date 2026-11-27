import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from mcp_server.test_snapshot import SnapshotError, create_snapshot


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _write(root / "app.py", "print('v1')\n")
    _write(root / "lib" / "util.py", "X = 1\n")
    _write(root / "old_name.py", "OLD = True\n")
    _write(root / "gone.py", "GONE = True\n")
    _write(root / ".gitignore", "*.log\nignored_dir/\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _members(snapshot) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(snapshot.archive_path) as tar:
        return {m.name: m for m in tar.getmembers()}


def _content(snapshot, name: str) -> bytes:
    with tarfile.open(snapshot.archive_path) as tar:
        return tar.extractfile(name).read()


def test_clean_tree_contains_committed_files(repo, tmp_path):
    snap = create_snapshot(repo, {}, tmp_path / "out")
    assert set(_members(snap)) == {".gitignore", "app.py", "lib/util.py", "old_name.py", "gone.py"}
    assert snap.file_count == 5
    assert snap.byte_count == sum(len((repo / name).read_bytes()) for name in _members(snap))
    assert snap.archive_path.parent == (tmp_path / "out").resolve()
    assert snap.sha256 == hashlib.sha256(snap.archive_path.read_bytes()).hexdigest()


def test_staged_unstaged_deleted_renamed_and_untracked_changes(repo, tmp_path):
    _write(repo / "app.py", "print('staged')\n")
    _git(repo, "add", "app.py")
    _write(repo / "app.py", "print('unstaged on top')\n")          # worktree wins over index
    _write(repo / "lib" / "util.py", "X = 2\n")                     # unstaged only
    (repo / "gone.py").unlink()                                      # unstaged deletion
    _git(repo, "mv", "old_name.py", "new_name.py")                  # staged rename
    _write(repo / "fresh.py", "NEW = 1\n")                          # untracked
    _write(repo / "staged_new.py", "S = 1\n")
    _git(repo, "add", "staged_new.py")
    _write(repo / "staged_new.py", "S = 2\n")

    snap = create_snapshot(repo, {}, tmp_path / "out")
    names = set(_members(snap))
    assert names == {".gitignore", "app.py", "lib/util.py", "new_name.py", "fresh.py", "staged_new.py"}
    assert _content(snap, "app.py") == b"print('unstaged on top')\n"
    assert _content(snap, "lib/util.py") == b"X = 2\n"
    assert _content(snap, "new_name.py") == b"OLD = True\n"
    assert _content(snap, "staged_new.py") == b"S = 2\n"
    # The caller checkout and index are unchanged by snapshotting.
    assert set(_git(repo, "status", "--porcelain").splitlines()) == {
        "MM app.py", " D gone.py", " M lib/util.py", "R  old_name.py -> new_name.py",
        "AM staged_new.py", "?? fresh.py"}


def test_ignored_files_are_excluded_unless_explicitly_included(repo, tmp_path):
    _write(repo / "debug.log", "noise\n")
    _write(repo / "ignored_dir" / "data.txt", "data\n")
    _write(repo / "ignored_dir" / "nested" / "more.txt", "more\n")
    snap = create_snapshot(repo, {}, tmp_path / "out")
    assert not {"debug.log", "ignored_dir/data.txt"} & set(_members(snap))

    snap = create_snapshot(repo, {"snapshot": {"include": ["ignored_dir"]}}, tmp_path / "out2")
    names = set(_members(snap))
    assert {"ignored_dir/data.txt", "ignored_dir/nested/more.txt"} <= names
    assert "debug.log" not in names


@pytest.mark.parametrize("path", [
    ".venv/lib/site.py", "venv/bin/activate", "node_modules/pkg/index.js",
    "__pycache__/app.cpython-311.pyc", "lib/__pycache__/util.pyc", ".pytest_cache/v/x",
    "build/out.o", "dist/pkg.whl", "pkg.egg-info/PKG-INFO", ".tox/py/x", ".mypy_cache/x",
    ".env", ".env.local", "secrets/id_rsa", "deploy/server.pem", "certs/client.key",
    ".npmrc", ".pypirc", ".netrc", "credentials.json", "gcp-credentials.json",
    ".mcp.json", ".claude/settings.local.json", ".codex/config.toml",
    ".cursor/mcp.json", "terminal-policy.json", "config/terminal-policy.json",
])
def test_default_excludes_apply_to_untracked_and_tracked_files(repo, tmp_path, path):
    _write(repo / path, "secret-or-build\n")
    _git(repo, "add", "-f", path)
    snap = create_snapshot(repo, {}, tmp_path / "out")
    assert path not in set(_members(snap))
    assert "app.py" in set(_members(snap))


@pytest.mark.parametrize("include", [".git", ".git/config", "lib/.git"])
def test_git_directory_can_never_be_included(repo, tmp_path, include):
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {"snapshot": {"include": [include]}}, tmp_path / "out")
    snap = create_snapshot(repo, {}, tmp_path / "ok")
    assert not any(".git" in name.split("/") for name in _members(snap))


def test_profile_excludes_and_include_overrides_default(repo, tmp_path):
    _write(repo / "fixtures" / "big.bin", "x" * 10)
    _write(repo / "build" / "keep.txt", "needed\n")
    _write(repo / "ignored_dir" / "sample.txt", "test fixture\n")
    profile = {"snapshot": {"exclude": ["fixtures/*.bin", "lib"],
                            "include": ["build/keep.txt", "ignored_dir/sample.txt"]}}
    snap = create_snapshot(repo, profile, tmp_path / "out")
    names = set(_members(snap))
    assert "fixtures/big.bin" not in names
    assert "lib/util.py" not in names
    assert {"build/keep.txt", "ignored_dir/sample.txt"} <= names


def test_explicit_includes_keep_sensitive_components_private(repo, tmp_path):
    protected = [".env.test", "nested/.AWS/sample.txt", ".claude/settings.local.json",
                 "certs/test.pem", ".codex/prefs.toml", "ops/policy.txt",
                 "nested/.vscode/mcp.json", ".a2a_link/private.json", ".claude/settings.json"]
    for rel in protected:
        _write(repo / rel, "fixture")
    snap = create_snapshot(repo, {"snapshot": {"include": protected,
                                               "policy_files": ["ops/policy.txt"]}}, tmp_path / "out")
    assert set(protected).isdisjoint(_members(snap))


def test_configured_policy_files_inside_project_are_excluded(repo, tmp_path, monkeypatch):
    _write(repo / "ops" / "my-policy.json", "{}\n")
    _write(repo / "ops" / "profiles.json", "{}\n")
    _write(repo / "ops" / "extra.json", "{}\n")
    monkeypatch.setenv("A2A_LINK_POLICY", str(repo / "ops" / "my-policy.json"))
    monkeypatch.setenv("A2A_LINK_PROFILE_CONFIG", str(repo / "ops" / "profiles.json"))
    profile = {"snapshot": {"policy_files": ["ops/extra.json"]}}
    snap = create_snapshot(repo, profile, tmp_path / "out")
    names = set(_members(snap))
    assert not {"ops/my-policy.json", "ops/profiles.json", "ops/extra.json"} & names


@pytest.mark.parametrize("bad", ["../outside", "lib/../../outside", "/etc/passwd",
                                 "C:/Windows/win.ini", "C:relative", "\\\\server\\share", ""])
def test_include_paths_cannot_escape_project_root(repo, tmp_path, bad):
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {"snapshot": {"include": [bad]}}, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_snapshot_config_must_be_well_formed(repo, tmp_path):
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {"snapshot": {"include": "lib"}}, tmp_path / "out")
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {"snapshot": {"exclude": [1]}}, tmp_path / "out")


def _symlink_or_skip(link: Path, target: str, target_is_directory=False):
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def test_symlinks_inside_root_are_kept_as_links_and_escaping_links_dropped(repo, tmp_path):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("do not copy\n")
    _symlink_or_skip(repo / "inside_link.py", "app.py")
    _symlink_or_skip(repo / "escape_link.txt", str(outside))
    _symlink_or_skip(repo / "relative_escape.txt", "../outside_secret.txt")
    snap = create_snapshot(repo, {}, tmp_path / "out")
    members = _members(snap)
    assert members["inside_link.py"].issym()
    assert members["inside_link.py"].linkname == "app.py"
    assert "escape_link.txt" not in members
    assert "relative_escape.txt" not in members
    with tarfile.open(snap.archive_path) as tar:
        for member in tar.getmembers():
            if member.isfile():
                assert b"do not copy" not in tar.extractfile(member).read()


@pytest.mark.parametrize("rel,target,expected", [
    ("a.py", "b.py", "b.py"),
    ("pkg/a.py", "../b.py", "../b.py"),
    ("pkg/a.py", "..\\b.py", None),
    ("a.py", "NUL.txt", None),
    ("a.py", "part./child", None),
    ("a.py", "../outside", None),
    ("pkg/a.py", "../../outside", None),
    ("a.py", "/etc/passwd", None),
    ("a.py", "C:/Windows/win.ini", None),
    ("a.py", ".git/config", None),
    ("a.py", "", None),
])
def test_symlink_targets_must_stay_inside_root(rel, target, expected):
    from mcp_server.test_snapshot import _safe_link_target
    assert _safe_link_target(rel, target) == expected


def test_include_through_symlinked_directory_cannot_escape(repo, tmp_path):
    outside = tmp_path / "outside_dir"
    _write(outside / "secret.txt", "do not copy\n")
    _symlink_or_skip(repo / "linked_dir", str(outside), target_is_directory=True)
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {"snapshot": {"include": ["linked_dir/secret.txt"]}}, tmp_path / "out")


def test_snapshot_hash_is_stable_across_runs_mtimes_and_clone_locations(repo, tmp_path):
    _write(repo / "fresh.py", "NEW = 1\n")
    first = create_snapshot(repo, {}, tmp_path / "a")
    os.utime(repo / "app.py", (1_000_000, 1_000_000))
    second = create_snapshot(repo, {}, tmp_path / "b")
    assert first.sha256 == second.sha256
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()

    copy = tmp_path / "elsewhere" / "repo"
    shutil.copytree(repo, copy)
    third = create_snapshot(copy, {}, tmp_path / "c")
    assert third.sha256 == first.sha256

    _write(repo / "fresh.py", "NEW = 2\n")
    assert create_snapshot(repo, {}, tmp_path / "d").sha256 != first.sha256


def test_destination_inside_project_is_not_captured(repo):
    first = create_snapshot(repo, {}, repo / "snapshots")
    second = create_snapshot(repo, {}, repo / "snapshots")
    assert second.sha256 == first.sha256
    assert not any(name.startswith("snapshots/") for name in _members(second))


def test_repository_root_destination_keeps_project_files_and_stores_output_separately(repo, tmp_path):
    for name in (".snapshot-" + "a" * 32 + ".tmp", "snapshot-" + "a" * 64 + ".tar",
                 "fixtures/snapshot-" + "a" * 64 + ".tar"):
        _write(repo / name, "project fixture")
    external = create_snapshot(repo, {}, tmp_path / "external")
    first = create_snapshot(repo, {}, repo)
    second = create_snapshot(repo, {}, repo)
    assert first.archive_path == second.archive_path == repo / ".a2a-link-snapshots" / external.archive_path.name
    assert second.sha256 == external.sha256
    assert set(_members(second)) == set(_members(external))
    after_root_capture = create_snapshot(repo, {}, tmp_path / "external-after-root")
    assert after_root_capture.sha256 == external.sha256
    assert {name for name in _members(second) if name.startswith(".snapshot-") or name.startswith("snapshot-")} == {
        ".snapshot-" + "a" * 32 + ".tmp", "snapshot-" + "a" * 64 + ".tar"
    }


def test_archive_entries_are_normalized_and_sorted(repo, tmp_path):
    _write(repo / "z.py", "z\n")
    _write(repo / "a" / "b.py", "b\n")
    snap = create_snapshot(repo, {}, tmp_path / "out")
    with tarfile.open(snap.archive_path) as tar:
        members = tar.getmembers()
    names = [m.name for m in members]
    assert names == sorted(names)
    for member in members:
        assert member.mtime == 0
        assert member.uid == 0 and member.gid == 0
        assert member.uname == "" and member.gname == ""
        assert not member.name.startswith("/") and ".." not in member.name.split("/")
        if member.isfile():
            assert member.mode in (0o644, 0o755)


def test_executable_mode_is_preserved(repo, tmp_path):
    # POSIX reads the worktree exec bit; Windows has none and uses the index mode.
    _write(repo / "run.sh", "#!/bin/sh\necho hi\n")
    os.chmod(repo / "run.sh", 0o755)
    _git(repo, "add", "run.sh")
    _git(repo, "update-index", "--chmod=+x", "run.sh")
    snap = create_snapshot(repo, {}, tmp_path / "out")
    members = _members(snap)
    assert members["run.sh"].mode == 0o755
    assert members["app.py"].mode == 0o644


def test_subdirectory_cwd_snapshots_whole_repository(repo, tmp_path):
    snap = create_snapshot(repo / "lib", {}, tmp_path / "out")
    assert {"app.py", "lib/util.py"} <= set(_members(snap))


@pytest.mark.parametrize("edit", ["delete", "overwrite", "add", "stage"])
def test_changes_during_capture_prevent_publication(repo, tmp_path, monkeypatch, edit):
    addfile = tarfile.TarFile.addfile

    def edit_after_first_file(tar, member, fileobj=None):
        addfile(tar, member, fileobj)
        if member.name == "app.py":
            if edit == "delete":
                (repo / "gone.py").unlink()
            elif edit == "overwrite":
                _write(repo / ".gitignore", "*.tmp\n")
            elif edit == "add":
                _write(repo / "new.py", "new")
            else:
                _git(repo, "update-index", "--chmod=+x", "gone.py")

    monkeypatch.setattr(tarfile.TarFile, "addfile", edit_after_first_file)
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {}, tmp_path / "out")
    assert not list((tmp_path / "out").iterdir())


def test_archive_limit_includes_headers_and_padding(repo, tmp_path):
    with pytest.raises(SnapshotError, match="max_bytes"):
        create_snapshot(repo, {"snapshot": {"max_bytes": 10239}}, tmp_path / "out")
    assert not list((tmp_path / "out").iterdir())
    snap = create_snapshot(repo, {"snapshot": {"max_bytes": 10240}}, tmp_path / "ok")
    assert snap.archive_path.stat().st_size == 10240


def test_large_source_is_streamed(repo, tmp_path):
    import tracemalloc
    with (repo / "large.bin").open("wb") as data:
        data.truncate(12 << 20)
    tracemalloc.start()
    try:
        snap = create_snapshot(repo, {}, tmp_path / "out")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert snap.byte_count >= 12 << 20
    assert peak < 6 << 20


@pytest.mark.parametrize("bad", ["dir\\name", "reserved/NUL.txt", "trailing.", "CONIN$",
                                 "ABCD~1.TXT"])
def test_nonportable_git_member_is_rejected(repo, tmp_path, bad):
    # Add just an index entry, even on filesystems that cannot create this spelling.
    oid = _git(repo, "rev-parse", "HEAD:app.py")
    _git(repo, "-c", "core.protectNTFS=false", "update-index", "--add", "--cacheinfo",
         f"100644,{oid},{bad}")
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {}, tmp_path / "out")


@pytest.mark.parametrize("directory", ["build", "generated"])
def test_excluded_nonportable_name_does_not_abort_capture(repo, tmp_path, directory):
    profile = {"snapshot": {"exclude": ["generated"]}}
    expected = create_snapshot(repo, profile, tmp_path / "before")
    oid = _git(repo, "rev-parse", "HEAD:app.py")
    _git(repo, "-c", "core.protectNTFS=false", "update-index", "--add", "--cacheinfo",
         f"100644,{oid},{directory}/non:portable")
    captured = create_snapshot(repo, profile, tmp_path / "after")
    assert captured.sha256 == expected.sha256


def test_ambiguous_hardlinked_file_is_rejected(repo, tmp_path):
    os.link(repo / "app.py", repo / "another.py")
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {}, tmp_path / "out")


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_directory_junction_is_rejected_without_symlink_privilege(repo, tmp_path):
    import _winapi
    original = repo / "original"
    (repo / "lib").rename(original)
    _winapi.CreateJunction(str(original), str(repo / "lib"))
    with pytest.raises(SnapshotError):
        create_snapshot(repo, {}, tmp_path / "out")


def test_non_git_directory_is_rejected(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("a\n")
    with pytest.raises(SnapshotError):
        create_snapshot(plain, {}, tmp_path / "out")
