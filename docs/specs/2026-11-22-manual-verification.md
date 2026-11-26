# A2A_Link: manual verification runbook

How to check the core slice end to end on a Windows machine. Results are recorded under "Observed".

## 1. Policy

The policy lives outside the project and outside `%APPDATA%`. The Codex desktop app is MSIX-packaged, and its MCP child process read a different version of a policy file stored under `AppData\Roaming`.

`%USERPROFILE%\.a2a_link\terminal-policy.json`: a copy of `config/terminal-policy.example.json` with `allowed_roots` set to your project directory and `audit_log` set to a file under your user-level `.a2a_link` directory.

## 2. Register the MCP server

```powershell
$Repo = (Resolve-Path .).Path
codex mcp add a2a-link --env A2A_LINK_POLICY=$env:USERPROFILE\.a2a_link\terminal-policy.json --env PYTHONPATH=$Repo -- $Repo\.venv\Scripts\python.exe -m mcp_server
```

```powershell
$Repo = (Resolve-Path .).Path
claude mcp add a2a-link -s user -e A2A_LINK_POLICY=$env:USERPROFILE\.a2a_link\terminal-policy.json -e PYTHONPATH=$Repo -- $Repo\.venv\Scripts\python.exe -m mcp_server
```

Delegated tasks can take minutes, which is longer than an MCP client's default tool timeout. For Codex, add `tool_timeout_sec = 900` under `[mcp_servers.a2a-link]` in `~/.codex/config.toml`. On the server side, `A2A_LINK_DELEGATE_TIMEOUT_S` (default 600) caps how long the delegated CLI may run.

Restart the CLI after any change. The policy is read once when the server starts.

## 3. Start the balancer

```powershell
.\.venv\Scripts\python.exe -m balancer.app
```

The dashboard is at http://127.0.0.1:8000/dashboard. The database defaults to `a2a_link.db` in the current directory; override it with `A2A_LINK_DB`.

## 4. Checks

| # | Action | Expected |
|---|---|---|
| T1 | `describe_terminal_policy` | `enabled: true`, roots, commands |
| T2 | `run_command ["git","status","--short"]` | `ok`, rc 0, audit `executed` |
| T3 | `run_command ["git","add","--dry-run","."]`, approve | prompt shown; audit `confirmed` then `executed` |
| T4 | same, decline / non-interactive client | `declined`, nothing run |
| T5 | `run_command ["git","push"]` | `denied` (`rule git push`) |
| D1 | `delegate_to_codex` with a prompt containing `" & % < >` | exact reply; balancer task `done` |
| D2 | `delegate_to_claude`, same prompt | exact reply; balancer task `done` |

## Observed (2026-11-22, Windows 11, codex-cli 0.167/0.171, Claude Code 2.1.330)

- **T1, Codex:** OK. Codex called `mcp__a2a_link__describe_terminal_policy` and summarised the rules correctly.
- **T2, Codex:** first failed with an opaque "Error executing tool run_command". Cause: the server read a policy with the example `C:/Users/YOU/...` audit path because of the `AppData` redirection above. Fixed by moving the policy to `%USERPROFILE%\.a2a_link` and by validating `audit_log` at start-up. After the fix: `ok`, rc 0.
- **T3, Codex:** first auto-cancelled. Cause: Codex rejects an elicitation schema with a top-level `title` ("unknown field `title`"). Fixed by stripping the title. With `codex -a on-request`, the prompt was shown; after approval the audit log has `confirmed` then `executed`, rc 0. With `approval_policy = "never"`, Codex auto-declines (`declined`), which is correct fail-closed behaviour.
- **T5, Codex (`codex exec`):** `denied`, `rule git push`.
- **T2/T4/T5, Claude Code (`claude -p --allowedTools mcp__a2a-link__run_command`):** `ok` rc 0 / `declined` (no UI for approval in `-p`) / `denied`. All three are in the audit log.
- **D1:** first failed by design: `codex` is an npm `.cmd` shim, and the executor refused prompts with cmd metacharacters. Fixed by unwrapping npm shims to `node <cli.js>`, so no `cmd.exe` is involved. After the fix, `delegate_to_codex` returned `PONG "a&b" 100% <ok>` in 12 s, and the task reached `done`.
- **D2:** `claude` is not on PATH (`~\.local\bin\claude.exe`); the executor now finds it there. `delegate_to_claude` returned the exact string in 8 s, and the task reached `done`.
- **Dashboard:** both tasks were listed live via SSE, with special characters escaped correctly.

### Async delegation, permissions and loop guard (same day)

Each agent was given a separate empty project directory and asked to create `hello.txt`, then to report whether any tool named `delegate_to*` was visible. The call used `wait_s=1`, so the result had to come through `get_delegation_result`.

- **Codex:** the first call returned `in_progress` after 1.4 s. `done` came after 35 s (2 polls; an earlier run took 73 s, which is beyond a 60 s tool timeout and still worked). `hello.txt` = `HELLO FROM CODEX`, created under `-s workspace-write`. Reply `TOOLS=NO`. The first run returned an empty reply because Codex's stdout was empty; it is now read from `codex exec -o`.
- **Claude:** `in_progress` after 1.3 s, then `done` after 13 s. `hello.txt` = `HELLO FROM CLAUDE`, created under `--permission-mode acceptEdits`. It reported that no tool contains `delegate_to`.
- **Balancer:** all three tasks `done`. `GET /tasks/{id}` returns the stored `result`.

Not yet checked: an interactive approval prompt inside Claude Code (only `-p` was tested), and a delegated agent that runs shell commands (tests) via `A2A_LINK_CLAUDE_ALLOWED_TOOLS`.

### Worktree isolation, optional balancer, delegated tests (same day)

Two throwaway git repositories, each with `calc.add` returning `a - b` and a failing `unittest`. Prompt: "Fix the bug in calc.py, then run `python -m unittest` to confirm it passes." The balancer was **not** running, and isolation was the default `auto`.

- Both calls returned `in_progress` immediately, with `balancer: "unreachable: task not recorded"`, and still finished: Claude in 20 s, Codex in 130 s.
- Each child ran in its own worktree under `A2A_LINK_WORKTREE_DIR`, on branch `a2a/<task_id>`. In both caller checkouts, `calc.py` was unchanged (`return a - b`). On the branch, `return a + b` was committed as `a2a: delegated task …`, and the diffstat was `calc.py | 2 +-`.
- Delegated Claude ran `python -m unittest` itself (allowed by the default `A2A_LINK_CLAUDE_ALLOWED_TOOLS`). It saw the test fail before the fix and pass after. Codex ran it under `workspace-write`.
- The suggested `git merge a2a/<id>` and `git worktree remove --force … && git branch -D a2a/<id>` worked on the Claude repository.
- `run_command ["python","-m","pytest",...]` now resolves to the pinned `.venv\Scripts\python.exe` from the policy, not to the system Python.

### v2 issue MVP: balancer checks (manual guidance)

The balancer half of the issue MVP is covered by `tests/balancer/test_issues.py`. These steps check it by hand and cover opening an issue from a Codex or a Claude chat. The adapter side (starting and resuming the CLIs, the supervisor) is a different track, so the "live client" rows stay empty until that track lands.

Start the balancer as in section 3 and open <http://127.0.0.1:8000/dashboard>. In PowerShell (replace the sha with `git rev-parse HEAD` of the repository under work):

```powershell
$b = "http://127.0.0.1:8000"
$sha = (git rev-parse HEAD)
$issue = Invoke-RestMethod -Method Post "$b/issues" -ContentType "application/json" -Body (@{
  request_id = "manual-1"; title = "Manual check"; goal = "Try the issue API"; acceptance = @("both participants ready")
  cwd = (Get-Location).Path; base_sha = $sha; opened_from = "claude" } | ConvertTo-Json)
$issue.paths
```

| # | Action | Expected |
|---|---|---|
| I1 | Run the snippet with `opened_from = "claude"`, then again with `"codex"` and a new `request_id` | `201` each; the snapshot shows `opened_from`; both participants `generation 1`, `handle null`; paths are strings and the directories do **not** exist (the adapter side creates them) |
| I2 | Repeat the same body | `200` with the same issue id; a changed `title` with the same `request_id` gives `409 request_id_conflict` |
| I3 | Add `caller_handle` (or any unknown field) to the body | `422`; there is no way to attach an already-open chat |
| I4 | Dashboard | the issue appears with goal, acceptance, round 1 and its base, both participants, and no tasks are affected |
| I5 | Post a `start` message for `claude`, save a handle, post a `done` result (see the contract, sections 6, 8 and 9) | participant shows its session id; confirmed effort shows "nie potwierdzono" |
| I6 | Post a second message to a thread that has an active turn | `409 active_turn` naming the active message |
| I7 | Leave an attempt running past `A2A_LINK_ATTEMPT_TTL_S` | the dashboard marks the participant `ZABLOKOWANY: owner_lost_suspected`; retry and abandon give `409 termination_unconfirmed` until `termination_confirmed` is posted |
| I8 | Mark both participants ready with different commits, propose, accept, start an integration with `verification_argv`, post candidate and passing tests, advance, post the result | the base moves to the candidate, round 2 opens, the issue stays `open`; `close` is a separate call |
| I9 | Start an integration with `"verification_argv": []` | accepted, but a `pass` result and `advance` return `409 verification_empty` |
| I10 | Restart the balancer in the middle of I7 or I8 | the same issue, attempts, leases and integration are shown again; a heartbeat from the owner renews the lease |

Opening an issue **from Codex or from Claude** is the same call; only `opened_from` differs. The chat that opens it stays the coordinator: it creates the issue, posts proposals and decisions with `by: "coordinator"`, and reads snapshots. Link starts the two managed CLI conversations in the returned worktrees, so no session of the chat is shared or resumed. (Observed in M1: an already-open Claude session cannot be auto-attached.)

**Normal A2A v1 delegation must still work next to issues.** With the same balancer running, repeat D1 and D2 above (`delegate_to_codex`, `delegate_to_claude`): the task appears in the task list with its log, reaches `done`, and `/usage`, `/incidents` and the usage bars are unchanged. `GET /events?once=true` now also carries an `issues` array next to `tasks`, `usage` and `incidents`.

Observed on the Claude track: **not yet.** The balancer code and tests in this change were written without the ability to run Python in the authoring session, so no test run, live client run or dashboard screenshot is recorded here. Record the results of `python -m pytest -q tests/balancer`, the full suite and rows I1 to I10 below when they have been run.

### Live progress (same day)

Same two-bug `calc.py` task, delegated to Codex and then Claude while the dashboard was open.

- **Dashboard:** the running task shows a ticking timer (`00:44 ⏱`). Clicking it opens the prompt and a live log, refreshed every 2 s, with lines such as `-> shell: python -m unittest`, `<- exit 1: …`, `say: …`, each stamped with the time since start. Claude's 5h bar appeared automatically (71%) from the `rate_limit_event` in its stream.
- **First attempt:** Codex's plain stderr echoed the whole prompt, including the user's global `AGENTS.md`, plus full command output (600+ lines for one command), with cp1252 mojibake. Switched to `codex exec --json`, which gives clean events and correct UTF-8. The mojibake repair remains for any raw lines.
- **Something the log revealed:** inside Codex's Windows `workspace-write` sandbox, `python` is not on PATH ("The term 'python' is not recognized"), so Codex looks for another interpreter. This is Codex's environment, not A2A_Link's. If a project needs a specific Python, name it in the prompt or in the project's own instructions.
- **After switching to `--json`:** Codex's log showed only steps, e.g. `-> shell: python -m unittest`, `<- exit 1: FF (+20 lines)`, `-> edit: …calc.py`, `say: …`, `-> shell: git diff --check`. Codex finished in 145 s and Claude in 24 s; both fixed both bugs on their own branch.
- **Codex usage windows:** read from the newest `~/.codex/sessions/…/rollout-*.jsonl` (`rate_limits.primary` 300 min, `secondary` 10080 min). The dashboard showed `codex 10% · reset 12:05` and `codex (7 dni) 2%` next to `claude 74%`.
