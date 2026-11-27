# Installing and upgrading A2A-Link 2.2.0

## Fresh installation

Follow the [Install](README.md#install) and [Use Link from any project](README.md#use-link-from-any-project) sections in the README. They create a virtual environment, install the package, and register the MCP server at user scope for Claude Code and Codex, so it is available in every project.

## Update an existing installation

Stop the balancer and test runner, close Claude Code and Codex, and save any work in the repository. Back up the directory configured by `A2A_LINK_HOME` before the first 2.2.0 start. The database, policy, profiles, logs, snapshots, and worktrees stay outside Git and are preserved by the upgrade.

### From 2.1.0 or newer

The checkout must be clean, on `main`, and have `origin` set to `https://github.com/Feijhoaaaaa/A2A-Link` (SSH URLs for that same repository are also accepted). The updater refuses local changes, other branches, or a different origin.

If you installed 2.1.0 from its `release/a2a-link-2.1.0-clean` branch, switch this clean checkout to the current `main` once before using the updater. Preserve or commit any local changes first. On Windows (PowerShell):

```powershell
git status --short
git fetch origin
git switch main
git pull --ff-only origin main
& .\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

If the clone has no local `main` branch, use `git switch --track -c main origin/main` instead of `git switch main`. On Linux or macOS, use the same Git commands and reinstall with `.venv/bin/python -m pip install -e '.[test]'`. Keep the old release branch; the switch does not delete it.

Windows (PowerShell), from the existing clone:

```powershell
& .\.venv\Scripts\a2a-link.exe update
```

Linux or macOS (bash), from the existing clone:

```bash
.venv/bin/a2a-link update
```

Alternatively, ask a connected Claude or Codex agent: **“Use the `update_a2a_link` tool to update A2A-Link from its official checkout. Report the installed version and restart requirements.”** The MCP tool runs the same checks and update as the CLI command.

### One-time update from 2.0.0

Version 2.0.0 does not include the self-updater yet. In the existing clone, fast-forward `main` and reinstall once:

Windows (PowerShell):

```powershell
git status --short
git switch main
git pull --ff-only origin main
& .\.venv\Scripts\python.exe -m pip install -e ".[test]"
& .\.venv\Scripts\python.exe -m pytest -q
```

Linux or macOS (bash):

```bash
git status --short
git switch main
git pull --ff-only origin main
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

If the checkout is dirty or `main` has diverged, stop and preserve the local changes. Do not reset or discard them to make the updater proceed.

## After updating

Restart the balancer and runner, then reopen Claude Code and Codex so they load the updated MCP server. Keep each user-level MCP entry pointed at the same clone and virtual environment. Existing `A2A_LINK_HOME`, `A2A_LINK_DB`, policy, profiles, runner tokens and worktrees remain in place; the new SQLite fields are added through an additive migration.

The updater runs only when explicitly called. It does not update on startup or restart running processes. Installations on 2.1.0 and later can use `a2a-link update`, `agentctl update`, or the `update_a2a_link` MCP tool for future releases.

## Version 2.2.0 changes

- Optional Tailscale Serve access for the dashboard, validated against Tailscale identity and tailnet forwarding headers. Never expose it with Tailscale Funnel.
- Claude weekly (`seven_day`) usage in the dashboard and `/usage`, preserved separately from the 5-hour window.
- Copyable participant session IDs and commands to resume the same Claude Code or Codex CLI conversation from its saved worktree.
- The previous 2.1.0 features remain included, including issue collaboration, background test jobs, Windows/Linux runner support, and the on-demand updater.

## Verification and limits

- Windows full suite: **747 passed, 5 skipped, 14 warnings**.
- Tailnet dashboard and `/usage` endpoint returned HTTP 200 on the configured local Tailscale Serve port.
- A Claude weekly value appears after the next delegated Claude event reports that window; the dashboard never invents missing usage data.
- Linux runner execution has not been verified on a real Linux host in this release.
