# A2A-Link

**Local-first coordination for Claude Code and Codex CLI.** A2A-Link lets the two coding agents delegate work, collaborate on persistent issues, and review changes through an MCP server backed by a local FastAPI balancer and Git worktrees.

The balancer stores state in SQLite and serves a live dashboard at `http://127.0.0.1:8000/dashboard`. It binds to localhost; task data and logs stay on your machine.

## What it does

- **Agent delegation:** Ask Claude Code or Codex to hand a task to the other CLI. Follow progress and collect the result in the original chat.
- **Persistent issue collaboration:** Open an issue with a goal and acceptance criteria, assign both agents, exchange messages and proposals, and integrate a verified candidate.
- **Isolated worktrees:** Managed agents work in separate Git worktrees so they do not edit the caller's checkout directly.
- **Live operations dashboard:** Inspect task status, logs, usage windows, issue progress, and incidents.
- **Local terminal tool:** Optionally let an agent run commands through an explicit allowlist and human approvals.

## Requirements

- Python 3.11 or newer
- Git 2.30 or newer, available on `PATH`
- At least one of Claude Code or Codex CLI installed and authenticated; delegation to the other agent requires that CLI too
- Claude Code or Codex CLI as the MCP host

## Install from GitHub

Clone the repository and install its dependencies in a virtual environment.

### Windows (PowerShell)

```powershell
git clone https://github.com/Feijhoaaaaa/A2A-Link.git
Set-Location A2A-Link
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

### macOS or Linux (Bash)

```bash
git clone https://github.com/Feijhoaaaaa/A2A-Link.git
cd A2A-Link
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

To install development tools as well, use `pip install -e ".[test]"` (PowerShell) or `pip install -e '.[test]'` (Bash).

## Configure the MCP server

The MCP server is user-level in these examples, so it is available from any project in the client. Replace `<REPO>` with the absolute path of your clone. Keep the policy and profile files outside the repository; do not commit personal settings.

### 1. Prepare local configuration

Copy the examples into your A2A-Link user directory and edit both `allowed_roots` and `audit_log` in `terminal-policy.json` to absolute paths that exist on your machine. List only the project directories you want `run_command` to access. The terminal tool denies commands that are not explicitly allowed. The separate agent CLIs still apply their own permission and sandbox settings.

Windows (PowerShell):

```powershell
$LinkHome = Join-Path $env:USERPROFILE ".a2a_link"
New-Item -ItemType Directory -Force $LinkHome | Out-Null
Copy-Item config\terminal-policy.example.json "$LinkHome\terminal-policy.json"
Copy-Item config\agent-profiles.example.json "$LinkHome\agent-profiles.json"
$LinkHome
```

macOS or Linux (Bash):

```bash
mkdir -p "$HOME/.a2a_link"
cp config/terminal-policy.example.json "$HOME/.a2a_link/terminal-policy.json"
cp config/agent-profiles.example.json "$HOME/.a2a_link/agent-profiles.json"
```

Update `allowed_roots` in the policy with absolute paths on your machine. The example profile list reflects model/effort pairs observed during development; adjust it for the models available to your accounts. You can omit `A2A_LINK_PROFILE_CONFIG` if you only use normal delegation and do not use issue profile resolution.

### 2. Register A2A-Link in Claude Code

From the cloned repository, run:

Windows (PowerShell):

```powershell
$Repo = (Resolve-Path .).Path
$LinkHome = Join-Path $env:USERPROFILE ".a2a_link"
claude mcp add a2a-link -s user `
  -e "A2A_LINK_POLICY=$LinkHome\terminal-policy.json" `
  -e "A2A_LINK_PROFILE_CONFIG=$LinkHome\agent-profiles.json" `
  -e "A2A_LINK_DB=$LinkHome\balancer.sqlite3" `
  -- "$Repo\.venv\Scripts\python.exe" -m mcp_server
```

macOS or Linux (Bash):

```bash
REPO="$(pwd)"
LINK_HOME="$HOME/.a2a_link"
claude mcp add a2a-link --scope user \
  --env "A2A_LINK_POLICY=$LINK_HOME/terminal-policy.json" \
  --env "A2A_LINK_PROFILE_CONFIG=$LINK_HOME/agent-profiles.json" \
  --env "A2A_LINK_DB=$LINK_HOME/balancer.sqlite3" \
  -- "$REPO/.venv/bin/python" -m mcp_server
```

### 3. Register A2A-Link in Codex CLI

Windows (PowerShell):

```powershell
$Repo = (Resolve-Path .).Path
$LinkHome = Join-Path $env:USERPROFILE ".a2a_link"
codex mcp add a2a-link `
  --env "A2A_LINK_POLICY=$LinkHome\terminal-policy.json" `
  --env "A2A_LINK_PROFILE_CONFIG=$LinkHome\agent-profiles.json" `
  --env "A2A_LINK_DB=$LinkHome\balancer.sqlite3" `
  -- "$Repo\.venv\Scripts\python.exe" -m mcp_server
```

macOS or Linux (Bash):

```bash
REPO="$(pwd)"
LINK_HOME="$HOME/.a2a_link"
codex mcp add a2a-link \
  --env "A2A_LINK_POLICY=$LINK_HOME/terminal-policy.json" \
  --env "A2A_LINK_PROFILE_CONFIG=$LINK_HOME/agent-profiles.json" \
  --env "A2A_LINK_DB=$LINK_HOME/balancer.sqlite3" \
  -- "$REPO/.venv/bin/python" -m mcp_server
```

Restart the client after registering or updating the MCP server. The user-level registration makes A2A-Link available in other repositories; the selected project path is passed to a task when you ask an agent to work there.

## Open the dashboard

Start the balancer from the repository root. Use the same database path configured for MCP so the dashboard shows the shared task history.

Windows (PowerShell):

```powershell
$env:A2A_LINK_DB = "$env:USERPROFILE\.a2a_link\balancer.sqlite3"
.\.venv\Scripts\python.exe -m balancer.app
```

macOS or Linux (Bash):

```bash
export A2A_LINK_DB="$HOME/.a2a_link/balancer.sqlite3"
.venv/bin/python -m balancer.app
```

Then open <http://127.0.0.1:8000/dashboard>. The page updates as tasks and usage arrive. If an MCP tool has already started the balancer on port 8000, reuse that process instead of starting a second one.

## Implementation and contribution

See [CONTRIBUTING.md](CONTRIBUTING.md) for the code map, setup for development, local checks, security boundaries, and the implementation workflow. Architecture and issue-workflow decisions are documented in [`docs/specs/`](docs/specs/).

## Security

- The dashboard and API listen on `127.0.0.1` by default. Do not expose them to a public network without adding and reviewing an authentication layer.
- Keep `terminal-policy.json`, its audit log, profile overrides, and the balancer database outside the repository.
- `run_command` uses argv directly without a shell and denies unlisted commands. The policy is not an operating-system sandbox; configure each agent CLI's own sandbox and approvals too.
- Delegated CLIs run with their own credentials and filesystem permissions. Only delegate projects you are authorized to change.

## License

This project is licensed under the [MIT License](LICENSE).
