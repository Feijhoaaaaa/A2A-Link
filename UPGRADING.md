# Upgrading A2A-Link

## Update an existing GitHub clone

This update advances the existing `main` branch from A2A-Link 2.0.0 to 2.1.0. Keep using your current clone and virtual environment; do not clone over the existing directory. The SQLite database, policy, profiles, logs, snapshots, and worktrees live under `A2A_LINK_HOME` (normally `~/.a2a_link`), outside the Git checkout, and are not replaced by `git pull`.

Before updating, stop the balancer and test runner, close Codex and Claude Code, and save or commit any local changes in the repository. Back up the directory configured by `A2A_LINK_HOME` before the first start of the new version.

### Windows (PowerShell)

Run from your existing A2A-Link clone:

```powershell
git status --short
git switch main
git pull --ff-only origin main
& .\.venv\Scripts\python.exe -m pip install -e ".[test]"
& .\.venv\Scripts\python.exe -m pytest -q
```

### macOS or Linux (Bash)

Run from your existing A2A-Link clone:

```bash
git status --short
git switch main
git pull --ff-only origin main
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

After verification, start the balancer and runner again, then reopen Codex and Claude Code. Existing user-level MCP entries can keep pointing to the same clone and interpreter; restart the clients to load the updated server. Keep your existing `A2A_LINK_HOME`, `A2A_LINK_DB`, policy, and profile paths. The application initializes new SQLite tables and applies additive column migrations at startup.

After this version is installed, future official updates can be applied on demand with `a2a-link update` (or `agentctl update`) from that environment. An agent can perform the same operation through the `update_a2a_link` MCP tool when you explicitly ask it to update A2A-Link. The command does not update on ordinary startup and does not restart running services or clients.

If `git pull --ff-only` stops because your local `main` has diverged, do not reset or discard it. Preserve your changes, then compare them with the upstream `main` before updating.

## Install this update in a different checkout

If the MCP entry points to another clone or virtual environment, update that checkout using the steps above and run `pip install -e` against that checkout's `pyproject.toml`. Re-register the user-level MCP server only if its command still points to the old clone. Keep personal policy, profile, and database files outside Git.

## Version 2.1.0 highlights

- Persistent A2A issue collaboration with managed Codex CLI and Claude Code sessions.
- Local balancer dashboard and durable background test jobs that resume the originating managed chat when results arrive.
- Configurable runner-local test profiles, Windows/Linux runner support, and automatic worker-count substitution for `pytest-xdist` profiles.
- Existing v1 delegation tools and data remain available. Linux runner behavior is implemented but has not yet been verified on a real Linux host.
