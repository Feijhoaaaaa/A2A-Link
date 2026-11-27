# A2A_Link

Local-first coordination for Claude Code and Codex CLI. The balancer, its API and the dashboard listen on `127.0.0.1` only. The one exception is the small set of authenticated runner routes, which an outbound background test runner may reach over your own tailnet (see [Background test runners](#background-test-runners)).

- **Balancer** (`balancer/`): FastAPI + SQLite. Stores tasks, 5h-window usage, capabilities, incidents, issues and background test requests. Routes tasks by load, serves a live dashboard (`/dashboard`) and the `agentctl` CLI.
- **MCP server** (`mcp_server/`): one stdio server that both CLIs connect to natively. It exposes:
  - `delegate_to_claude(prompt, cwd?, wait_s?)` / `delegate_to_codex(...)`: delegate a whole task to the other agent.
  - `get_delegation_result(task_id, wait_s?)`: collect the result of a delegated task that is still running.
  - `get_delegation_log(task_id, lines?)`: live progress of a delegated task.
  - `list_delegations(limit?)`: recent delegations from every agent (who, to whom, status, time).
  - `run_command(argv, cwd, timeout_s?)`: the **local terminal executor**, limited to an allowlist.
  - `describe_terminal_policy()`: tells the agent which commands and directories are allowed.
  - `open_issue(...)`, `send_issue_message(...)`, and issue review/integration tools: keep Claude and Codex working in separate managed CLI sessions and isolated Git worktrees. Managed issue participants receive a restricted MCP server exposing `start_tests(profile, targets?)` and read-only request status, with issue context injected by Link.

- **Test runner** (`a2a-link-runner`, `mcp_server/test_runner.py`): claims background test jobs from the balancer and runs them from a verified snapshot, on the same machine or on another Windows/Linux machine in your tailnet.
- **Updater** (`a2a-link update` / `agentctl update`): fetches the official GitHub `main`, fast-forwards a clean clone, and reinstalls it into the current virtual environment. The same operation is available to an agent as the `update_a2a_link` MCP tool.

Design: [docs/specs/2026-11-22-orchestration-design.md](docs/specs/2026-11-22-orchestration-design.md).

## Install

Requirements: Python 3.11 or newer and git. Node is optional (only the dashboard behaviour tests use it; they are skipped without it).

Clone the repository from GitHub and install it into a virtual environment:

Windows (PowerShell):

```powershell
git clone https://github.com/Feijhoaaaaa/A2A-Link.git A2A_Link
cd A2A_Link
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"
.venv\Scripts\python.exe -m pytest -q
```

Linux (bash):

```bash
git clone https://github.com/Feijhoaaaaa/A2A-Link.git A2A_Link
cd A2A_Link
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

Already using A2A-Link 2.0.0? Follow the in-place update steps in [UPGRADING.md](UPGRADING.md). They keep the existing clone, MCP registration, runner configuration, and data directory.

The install provides two console scripts in the venv: `agentctl` (balancer CLI) and `a2a-link-runner` (test runner). In the examples below, `<repo>` is the absolute path of your clone and `<python>` is the venv interpreter (`<repo>\.venv\Scripts\python.exe` on Windows, `<repo>/.venv/bin/python` on Linux).

### Use Link from any project

Register the MCP server in each CLI's **user** configuration after installing the clone in editable mode. This makes the same Link installation available to every project you open in that CLI. Run the commands from this repository, and replace the example data directory if you keep Link state elsewhere.

If `a2a-link` is already configured, remove the old user entry before adding the new one: `codex mcp remove a2a-link` and `claude mcp remove a2a-link --scope user`.

Windows (PowerShell):

```powershell
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$linkHome = Join-Path $env:USERPROFILE ".a2a_link"
if (-not $env:CODEX_HOME) { $env:CODEX_HOME = Join-Path $env:USERPROFILE ".codex" }
codex mcp add a2a-link --env "A2A_LINK_HOME=$linkHome" --env "A2A_LINK_DB=$linkHome\balancer.sqlite3" -- $python -m mcp_server
claude mcp add --scope user --env "A2A_LINK_HOME=$linkHome" --env "A2A_LINK_DB=$linkHome\balancer.sqlite3" a2a-link -- $python -m mcp_server
```

Linux (bash):

```bash
python_path="$(realpath .venv/bin/python)"
link_home="$HOME/.a2a_link"
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
codex mcp add a2a-link --env "A2A_LINK_HOME=$link_home" --env "A2A_LINK_DB=$link_home/balancer.sqlite3" -- "$python_path" -m mcp_server
claude mcp add --scope user --env "A2A_LINK_HOME=$link_home" --env "A2A_LINK_DB=$link_home/balancer.sqlite3" a2a-link -- "$python_path" -m mcp_server
```

Restart the CLI after registration. `codex mcp list` and `claude mcp list` show the global server. To update it to a newer checkout, reinstall that checkout into the configured virtual environment (`<python> -m pip install -e "<repo>"`) and keep the server command pointed at that environment. The balancer is a separate local service; start it with `<python> -m balancer.app` when using the dashboard or background runners.

## Running the balancer and the dashboard

```bash
<python> -m balancer.app
```

The balancer binds `127.0.0.1:8000` and stores its state in `a2a_link.db` in the current directory (override with `A2A_LINK_DB`). Logs, snapshots and worktrees live under `A2A_LINK_HOME` (default `~/.a2a_link`, i.e. `%USERPROFILE%\.a2a_link` on Windows).

Open `http://127.0.0.1:8000/dashboard` in a browser on the same machine. The dashboard is a single self-contained page (no external scripts, fonts or CDNs) with:

- **Usage windows** and **Incidents**, live over the `/events` stream.
- **Test requests**: the newest 50 requests from `GET /test-requests?limit=50`, refreshed every 5 seconds. Selecting one shows each job's state, target/OS, runner ID and platform, the snapshot SHA-256, the requested resources while the job is queued or running, and the effective resources only once the runner has reported a result. Logs are read from the bounded log endpoint only when you expand them. **Cancel** appears only while some job can still be cancelled.
  - A **lost** job means its lease expired and the balancer could not prove the process stopped. The runner slot stays held and needs manual resolution; the job is never retried or reported as successful.
  - A finished request from a managed issue session shows its durable delivery state: pending, active, delivered, failed, or undeliverable. Results resume the same Link-managed CLI session when it is idle; the runner never receives provider session IDs or CLI credentials.
- **Delegated tasks** with prompt, live log and result, and **Issues** with participants, messages, proposals, decisions and integrations.

The dashboard and every non-runner route are loopback-only: the balancer rejects other peers, non-loopback `Host` headers, cross-origin state changes and requests that arrive through a proxy. Do not publish it through a tunnel or reverse proxy.

### Optional: local layout preview with Vercel

If you already use the Vercel CLI, `vercel dev --local` can serve `balancer/static/` as a local preview while you work on the page's layout. This is a local preview only: the page needs the balancer API, so data appears only when the page is served by the balancer itself. Do not run `vercel link` or `vercel deploy` for this repository, and do not create a hosted project or public URL for it. The real dashboard is always the one at `127.0.0.1`.

## Background test runners

An agent can ask for its tests to run in the background against an exact snapshot of its working tree, on one or more target OSes. The balancer queues one job per target; a runner claims a job whose target labels it has, downloads and verifies the snapshot, runs the test profile, streams a bounded log back, and posts one result. The API contract is [docs/superpowers/specs/2026-11-27-background-test-jobs-api.md](docs/superpowers/specs/2026-11-27-background-test-jobs-api.md); test profiles are described in [config/test-profiles.example.json](config/test-profiles.example.json).

There are two ways to run a runner:

| | Local runner | Outbound Tailscale runner |
|---|---|---|
| Where | the balancer host | another Windows or Linux machine in your tailnet |
| Balancer URL | `http://127.0.0.1:8000` | `https://<balancer-host>.<tailnet>.ts.net` (from `tailscale serve`) |
| Network | loopback only | the runner connects out to the balancer; nothing listens on the runner |

Either way, every runner call carries the runner's bearer token, and the balancer uses the runner ID and labels it registered, not what the runner claims.

### 1. Enable registration on the balancer host

Registration happens on the balancer host only (`POST /runners/register` is loopback-only). Choose a long random admin token and put it in the balancer's environment as `A2A_LINK_RUNNER_REGISTRATION_TOKEN` before starting it. Registration is refused while the variable is unset.

```powershell
# Windows: generate once, keep it in your user environment, restart the balancer afterwards
[Environment]::SetEnvironmentVariable("A2A_LINK_RUNNER_REGISTRATION_TOKEN", [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"), "User")
```

```bash
# Linux: e.g. in the balancer's systemd unit or a file sourced before starting it
export A2A_LINK_RUNNER_REGISTRATION_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

### 2. Register each runner (on the balancer host)

Each runner gets an ID, its platform (`windows` or `linux`) and its labels. **Required labels:** register every runner with its OS label, `windows` or `linux`, because test targets name the OS they need. Add extra labels (for example `gpu` or `x64`) only for targets that ask for them; a job is offered only to a runner whose registered labels include every label of its target.

The response contains the runner token exactly once; the balancer stores only its SHA-256 hash. The snippet below reads the admin token from the environment and writes the runner token straight to a file, so neither token appears on a command line or in shell history:

```bash
<python> - <<'EOF'
import os, pathlib, httpx
runner_id, platform, labels = "linux-1", "linux", ["linux"]
out = pathlib.Path.home() / ".a2a_link" / f"runner-{runner_id}.token"
r = httpx.post("http://127.0.0.1:8000/runners/register",
               headers={"Authorization": "Bearer " + os.environ["A2A_LINK_RUNNER_REGISTRATION_TOKEN"]},
               json={"runner_id": runner_id, "platform": platform, "labels": labels, "rotate": False})
r.raise_for_status()
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(r.json()["token"] + "\n", encoding="utf-8")
os.chmod(out, 0o600)
print("token written to", out)
EOF
```

On Windows, pipe the same script into Python with a PowerShell here-string (`@' ... '@ | <python> -`) and set `runner_id`, `platform`, `labels` to e.g. `"win-1"`, `"windows"`, `["windows"]`.

Registering an existing runner ID again requires `"rotate": true`. Rotation replaces the token and invalidates the old one; it is also how you revoke a runner.

Copy the token file to the runner machine over a channel you trust (for example `scp` or `tailscale file cp`) and keep it private: `chmod 600` on Linux; on Windows keep it under your user profile and remove inherited access if other users share the machine (`icacls <file> /inheritance:r /grant:r "%USERNAME%:R"`). Delete the copy on the balancer host if that host is not the runner.

### 3. Start the runner

The token is read from `--token-file` or from the `A2A_LINK_RUNNER_TOKEN` environment variable. It is never accepted as a command-line argument (`--token` is refused) and is removed from the environment before any test process starts.

Test requests carry a profile **name** only. Each runner resolves that name against its own trusted catalog, so Windows and Linux can use the same name with different local commands. The default is `$A2A_LINK_HOME/test-profiles.json` when present; otherwise a source checkout uses `config/test-profiles.example.json`. Copy the example and edit it on each runner:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.a2a_link" | Out-Null
Copy-Item config/test-profiles.example.json "$env:USERPROFILE\.a2a_link\test-profiles.json"
```

```bash
mkdir -p ~/.a2a_link
cp config/test-profiles.example.json ~/.a2a_link/test-profiles.json
```

Override the path with `A2A_LINK_TEST_PROFILE_CONFIG` or `--profiles-file`. Add only profile names you intend to allow, and set each runner's `argv`, environment and resource limits locally. A request with an unknown name is reported failed before the runner downloads the snapshot or starts a process. Inline command objects are rejected by the balancer.

Local runner on the balancer host (Windows):

```powershell
<repo>\.venv\Scripts\a2a-link-runner.exe --balancer-url http://127.0.0.1:8000 --runner-id win-1 --token-file $env:USERPROFILE\.a2a_link\runner-win-1.token
```

Outbound runner on a Linux machine in the tailnet:

```bash
<repo>/.venv/bin/a2a-link-runner --balancer-url https://<balancer-host>.<tailnet>.ts.net --runner-id linux-1 --token-file ~/.a2a_link/runner-linux-1.token
```

Other options: `--state-dir` (per-job work directories, default `$A2A_LINK_RUNNER_HOME` or `~/.a2a_link/runner`), `--profiles-file` (local allowlist override), `--lease-s` (15–600, default 60), `--cpu-count`, `--once` (run at most one job and exit when idle). A runner runs one job at a time.

### 4. Expose only the runner routes over Tailscale (outbound runners only)

The balancer keeps listening on `127.0.0.1`. On the balancer host, publish only the runner routes to your tailnet with `tailscale serve`:

```bash
tailscale serve --bg --set-path /runners http://127.0.0.1:8000/runners
tailscale serve --bg --set-path /test-jobs http://127.0.0.1:8000/test-jobs
tailscale serve status
```

`tailscale serve status` should list exactly these two paths. Do not serve `/`, `/dashboard`, `/events`, `/tasks`, `/issues` or `/test-requests`. Even if a non-runner route is reached through the proxy (including `/runners/register`), the balancer refuses it because it carries forwarding headers, but keep the serve configuration minimal anyway.

**Never use `tailscale funnel`** for A2A_Link: funnel publishes to the public internet. The runner routes are meant for your tailnet only, and the dashboard stays on `127.0.0.1`.

### Resources, failures and limits

- For Python projects, select `python-pytest-xdist` when the project has `pytest-xdist` installed. The runner substitutes the detected logical CPU count into `-n {jobs}` and reports the applied value; it never guesses parallel test flags. Windows and Linux may keep different trusted commands under that same profile name.
- A profile may request `parallelism` and a `memory_limit_mb`. The dashboard shows these as requested while a job runs, and shows the runner's reported `resources` (including `memory_limit_enforced` exactly as reported) after the result. Memory caps are enforced for the whole process tree on Windows (Job Objects); a Linux runner rejects a capped job before downloading it.
- Leases default to 60 seconds. If a runner stops renewing its lease, the job becomes **lost**: the balancer keeps the runner ownership, the log evidence and the held runner slot, and never marks the job successful or retries it. Check the runner machine and resolve it manually.
- Logs are bounded to 8 MiB per job (the beginning is kept); results carry stdout/stderr tails of up to 64 KiB each. There is no automatic cleanup of logs or snapshots yet.
- Linux execution has not been verified on a real Linux host in this release; see [docs/specs/2026-11-27-background-test-jobs-verification.md](docs/specs/2026-11-27-background-test-jobs-verification.md).

## Local terminal executor

Agents get real terminal operations without simulating keystrokes through the GUI. The executor uses the official MCP tool mechanism, so each call shows up as a normal tool call in the CLI's own chat. The CLI's own tool-permission prompts still apply.

### Security model

| Layer | What it does |
|---|---|
| No shell | `argv` goes straight to `CreateProcess`/`execve` (`shell=False`). There is no `eval` and no `sh -c`. Pipes, `&&` and redirections are plain arguments. |
| Allowlist | The policy JSON lists commands and argument-prefix rules with level `safe` / `confirm` / `deny`. Anything not listed is denied. |
| Hard deny | Shells and system tools (`cmd`, `powershell`, `bash`, `wsl`, `reg`, `schtasks`, `runas`, …) can never be allowlisted. `.bat`/`.cmd` shims are refused because `cmd.exe` would re-parse their arguments. |
| Directories | `cwd` and every path-like argument (absolute, `..`, `~`, `--opt=path`) must resolve, symlinks included, inside `allowed_roots`. |
| Confirmation | `confirm` commands ask the human through MCP **elicitation**: `Resolve`/`Elicit` from SDK 2.x, which works on both the 2026-07-28 and older protocols. Decline, cancel, or a client without elicitation means the command does not run (fail closed). |
| Isolation | Scrubbed environment (allowlisted variables only; API keys are not inherited), `stdin=DEVNULL`, its own process group, the whole process tree killed on timeout (`taskkill /T`, no admin needed), stdout/stderr capped. |
| Timeouts | `default_timeout_s`; requests are clamped to `max_timeout_s`. A timeout returns `status=timeout`, `returncode=124`. |
| Audit | Append-only JSONL with one line per decision (`denied` / `declined` / `confirmed` / `executed`). It records argv, cwd, level, reason, exit code, duration and output sizes, but not the output itself. |
| Policy integrity | The policy is loaded once at start-up and must live **outside** `allowed_roots`, so the agent cannot edit its own permissions. |

The result of `run_command` contains `status` (`ok|failed|timeout|denied|declined|error`), `returncode`, `stdout`, `stderr`, `timed_out`, `duration_s`, `*_truncated`, `level` and `reason`.

### Setup

1. Copy [config/terminal-policy.example.json](config/terminal-policy.example.json) outside the project, e.g. to `%USERPROFILE%\.a2a_link\terminal-policy.json` on Windows or `~/.a2a_link/terminal-policy.json` on Linux. Set `allowed_roots` and `audit_log` (e.g. `~/.a2a_link/terminal-audit.jsonl`).
   **On Windows, do not use `%APPDATA%`.** The Codex desktop app is an MSIX package, and Windows can redirect `AppData` for it and its child processes. In testing, the server launched by Codex read a different version of the policy than the one on disk.
2. Register the server, replacing `<repo>` with the absolute path of your clone.

Claude Code (Windows):

```powershell
claude mcp add a2a-link -s user -e A2A_LINK_POLICY=$env:USERPROFILE\.a2a_link\terminal-policy.json -e PYTHONPATH=<repo> -- <repo>\.venv\Scripts\python.exe -m mcp_server
```

Codex CLI (Windows):

```powershell
codex mcp add a2a-link --env A2A_LINK_POLICY=$env:USERPROFILE\.a2a_link\terminal-policy.json --env PYTHONPATH=<repo> -- <repo>\.venv\Scripts\python.exe -m mcp_server
```

On Linux use the same commands with `$HOME/.a2a_link/terminal-policy.json` and `<repo>/.venv/bin/python`.

Commands are looked up on the server's `PATH`. To pin a name to a specific program, e.g. so that `python` means the project venv rather than the system Python, add `"path"` to the command:

```json
"python": { "path": "<repo>/.venv/Scripts/python.exe", "default": "deny", "rules": [ ... ] }
```

A pinned path must be absolute and exist. It is still refused if it is a `.bat`/`.cmd` file or a shell/system tool.

### Limitations

- The confirmation is only as trustworthy as the MCP client's elicitation UI. If a client does not support elicitation, `confirm` commands are refused instead of running.
- Codex with `approval_policy = "never"` auto-declines every elicitation, so `confirm` commands never run. Start it with `codex -a on-request` to get the approval prompt.
- The policy limits only `run_command`. An agent's own built-in shell is governed by that agent's settings (e.g. Codex `sandbox_mode`), not by this policy.
- The policy is read once at server start. After editing it, restart the CLI so it relaunches the server.
- Path checking is heuristic: it inspects arguments that look like paths. Keep risky subcommands at `confirm` or `deny`, and deny flags that redirect a tool elsewhere (e.g. `git -C`, `--git-dir`, `-c`).
- This is not an OS sandbox. An allowed program runs with the user's rights, so allowlist only tools whose allowed subcommands you trust.

## Delegation

`delegate_to_codex(prompt, cwd?, wait_s?, isolation?)` / `delegate_to_claude(...)` log a task in the balancer and run `codex exec` / `claude -p` in the background.

**Automatic balancer startup.** Both delegation tools check the configured balancer before sending the task. If the local HTTP balancer is offline, the MCP process starts it and waits for readiness. It stays available while that MCP process runs. If the address is unusable or startup fails, the tool returns `needs_human`: ask the user for help and retry after the problem is resolved. No agent task runs without registration.

**Isolation in a git worktree.** The caller keeps working while the delegated agent runs, so by default (`isolation: "auto"`) the child does not touch the caller's checkout:

- When `cwd` is inside a git repository with at least one commit, the server creates a worktree from `HEAD` in `~\.a2a_link\worktrees\<repo>-<task_id>` (override with `A2A_LINK_WORKTREE_DIR`) on branch `a2a/<task_id>`. The child runs there and is told not to commit or switch branches.
- When the child finishes, its changes are committed on that branch, without skipping hooks. The result's `workspace` field gives the branch, `changes` (diffstat), and ready-made `merge` and `cleanup` commands.
- The worktree starts from the last commit, so uncommitted changes in the caller's checkout are **not** in it. The result warns when that is the case. Commit first if the child needs them.
- Outside a git repository, `auto` falls back to the shared directory, with a warning. `isolation: "worktree"` refuses instead; `isolation: "none"` always uses the shared directory.

**Asynchronous.** The tool waits up to `wait_s` seconds (default 45, max 55, i.e. under the usual 60 s MCP tool timeout). A fast task returns `status: done` with `result`. A longer one returns `status: in_progress` and a `task_id`; the caller collects it with `get_delegation_result(task_id)`, which waits up to `wait_s` per call. Results and errors are stored in the balancer (`GET /tasks/{id}`), so they survive an MCP server restart. A task still running in a server process that exits will not finish.

**No delegation loops.** The child agent starts without this MCP server: Claude with `--strict-mcp-config`, Codex with `-c mcp_servers.a2a-link.enabled=false`. As a second guard, the child gets `A2A_LINK_DELEGATION_DEPTH=1`, and the server refuses to delegate at depth ≥ `A2A_LINK_MAX_DELEGATION_DEPTH` (default 1).

**Permissions of the delegated agent** (it runs unattended, so nothing can be approved interactively):

| Variable | Default | Effect |
|---|---|---|
| `A2A_LINK_CLAUDE_PERMISSION_MODE` | `acceptEdits` | Claude may edit files; other tools need the allowlist below |
| `A2A_LINK_CLAUDE_ALLOWED_TOOLS` | tests and read-only git: `python -m pytest`, `pytest`, `python -m unittest`, `.venv` pytest, `git status/diff/log` | comma-separated Claude Code permission rules; set to an empty string for file edits only |
| `A2A_LINK_CODEX_SANDBOX` | `workspace-write` | Codex may write only inside `cwd` |

**Live progress.** While a delegated agent works, its progress is written to `~\.a2a_link\tasks\<task_id>.log` (base directory: `A2A_LINK_HOME`). Every line carries the time since the task started:

```
[00:02] delegated to codex (task d8147cd4e6f7)
[00:03] codex session started
[00:12] say: I'll run the tests first.
[00:18] -> shell: python -m unittest
[00:20] <- exit 1: F. (+12 lines)
[00:35] ... still working (no new output for 15s, elapsed 00:35)
[00:41] -> edit: calc.py
[00:52] finished: done after 00:52
```

- Claude runs with `--output-format stream-json --verbose` and Codex with `--json`. Their events are summarised as `say:` (the agent's messages), `-> tool: input` (a command or edit), `<-` (its result, first line only) and `thinking:`. Echoed prompts and full command output are left out.
- When the agent prints nothing for `A2A_LINK_HEARTBEAT_S` seconds (default 15), a `... still working` line is added, so a quiet task still shows signs of life.
- You can read the log in three places: the `get_delegation_log` tool from any agent, the dashboard (click a task: prompt, live log refreshed every 2 s, result or error, running timer), and `GET /tasks/{id}/log` on the balancer.
- `list_delegations` shows tasks from every connected agent while the balancer runs; without it, only the current session's tasks are listed.
- **Usage windows** (dashboard "Usage windows" and `GET /usage`):
  - **Claude:** its stream reports how much of the five-hour window is used. The MCP server forwards this to the balancer on each delegation, so the figure is as fresh as the last delegated Claude task.
  - **Codex:** `--json` does not report limits, but every Codex session writes them to `~/.codex/sessions/…/rollout-*.jsonl` (base dir: `CODEX_HOME`). The balancer reads the newest session file on each refresh and shows the 5h and 7-day windows with their reset times, including when you use Codex directly rather than through delegation. The 5h figure also feeds the router: an agent at 90% or more is not suggested for new tasks. This is read-only and stays local. Turn it off with `A2A_LINK_READ_CODEX_USAGE=0`.

**Context.** The delegated agent does not see the caller's conversation. The prompt is prefixed with a short preamble (it was delegated, the project directory, work autonomously, end with a summary of files changed, commands run and what is left). Disable it with `A2A_LINK_DELEGATE_PREAMBLE=0`. Put everything the other agent needs into `prompt`.

Other details:

- On Windows, npm `.cmd` shims (like `codex.cmd`) are unwrapped to `node <cli.js>`, so prompts never pass through `cmd.exe`.
- `claude` is also looked up in `~\.local\bin`. Override any binary with `A2A_LINK_CODEX_BIN` / `A2A_LINK_CLAUDE_BIN`.
- For Codex, the reply is read from `codex exec -o <file>` (its final message), because stdout can be empty after long runs.
- `A2A_LINK_DELEGATE_TIMEOUT_S` (default 600) caps how long a delegated run may take.

Set these variables in the MCP server's environment (`-e` / `--env` when registering it).

The end-to-end runbook with observed results is in [docs/specs/2026-11-22-manual-verification.md](docs/specs/2026-11-22-manual-verification.md).

## Issues (v2): Link-managed collaboration

An **issue** is a goal with acceptance criteria that two Link-managed CLI participants (`claude` and `codex`) work on in their own git worktrees. The contract between the balancer and the MCP/adapter side is [docs/specs/2026-11-24-issue-mvp-api.md](docs/specs/2026-11-24-issue-mvp-api.md); this section describes what the balancer does.

- **The chat that opens an issue is only the coordinator**, whether it is a Claude chat (`opened_from: "claude"`) or a Codex chat (`opened_from: "codex"`). Link starts a **fresh** CLI conversation for each participant in its assigned worktree and resumes it later from the persisted session id. An already-open chat is never attached; the API has no field for that and rejects unknown fields.
- **The balancer only stores state.** It never runs a CLI, never runs git and never creates directories. Worktree paths (`<A2A_LINK_HOME>/worktrees/<issue_id>-<key>`, branch `a2a/issue-<issue_id>/<key>`) are returned as strings for the adapter side to create.
- **Everything is transactional.** Each request runs in one `BEGIN IMMEDIATE` transaction on its own connection, with compare-and-swap version checks and partial unique indexes (one active turn per thread, one current ready per participant and round, one unresolved attempt per message, one open integration per issue). Several balancer processes can share one SQLite file (WAL mode).

| Area | Endpoints |
|---|---|
| Issue | `POST /issues` (idempotent on `request_id`), `GET /issues/{id}` (full snapshot), `POST /issues/{id}/close` |
| Participants | `PUT .../participants/{key}/handle`, `POST .../generation`, `POST/GET .../messages`, `POST .../ready` |
| Attempts | `POST /issues/{id}/attempts/{attempt_id}/process`, `/heartbeat`, `/termination_confirmed`, `/result`; `POST /issues/{id}/messages/{message_id}/retry`, `/abandon` |
| Decisions | `POST /issues/{id}/proposals`, `/proposals/{id}/revisions`, `/decisions` |
| Integration | `POST /issues/{id}/integrations`, then `/{integration_id}/process`, `/heartbeat`, `/termination_confirmed`, `/takeover`, `/candidate`, `/tests`, `/advance`, `/result` |

Things worth knowing:

- **Readiness uses distinct commits.** Each participant marks its own `source_commit_sha` ready; they are not compared. Integration merges exactly those commits into a third `candidate_sha`, runs the `verification_argv` given at integration start against that exact commit, and moves the base only if the tested sha is the candidate and the base is still the expected one. A successful integration opens the next round and leaves the issue open; closing is a separate call. An empty `verification_argv` is accepted at start but can never pass or advance.
- **A lapsed lease is only a suspicion.** An attempt whose lease expires becomes `owner_lost_suspected`: it keeps its thread, and retry, abandon, new messages and takeover stay refused until the local supervisor posts `termination_confirmed` for it. Time alone never frees anything. A balancer restart changes nothing; the owner's next heartbeat renews the lease. The supervisor itself (spawn journal, process identity, Windows Job Object) belongs to the adapter side.
- **Requested and confirmed profiles are separate.** What a CLI did not confirm (effort, context, limits) stays `null` and is shown as "not confirmed". A model that differs from the request is recorded and shown as "profile mismatch", never turned into a denial.
- **Dashboard.** `/dashboard` has an issues section: goal, acceptance criteria, round and base, both participants (handle, readiness SHA, active turn, blocked state), each message with its requested and confirmed profile, proposals, decisions, and integrations with candidate, test result and lease state.
- **Clock and TTL.** `A2A_LINK_ATTEMPT_TTL_S` (default 900) sets the lease length. Tests inject a clock through `create_app(db, clock=..., attempt_ttl_s=...)`.

Normal A2A v1 delegation (`delegate_to_codex` / `delegate_to_claude`, `/tasks`, `/usage`, `/incidents`, `/events`) is unchanged and works with or without issues.

## Developing A2A_Link with an agent that uses it

- **Restart after changing the server.** An MCP client starts `python -m mcp_server` once per session and keeps that process. When an agent edits `mcp_server/` or the policy, it keeps using the old code until the CLI is restarted. Run the tests (`python -m pytest`) to check a change. To try it live, restart the CLI or open a new session.
- **The safeguards are cooperative here.** While the agent works on this repository, the executor's own code is inside its allowed roots. An agent with its own unrestricted shell (e.g. Codex with `sandbox_mode = "danger-full-access"` and `approval_policy = "never"`) can change that code or bypass `run_command` entirely. The policy limits what goes through `run_command`. It is not a sandbox against the agent itself. For real containment, use the agent's own sandbox settings, e.g. Codex `workspace-write` with `on-request` approvals.

## License

MIT, see [LICENSE](LICENSE).
