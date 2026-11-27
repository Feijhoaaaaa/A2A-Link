# Changelog

## 2.2.0 — 2026-11-27

### Added

- Optional Tailscale Serve access for the dashboard, restricted to authenticated tailnet proxy requests; Tailscale Funnel remains unsupported.
- Claude seven-day usage reporting and display alongside the five-hour limit, using additive SQLite columns that preserve existing usage data.
- Dashboard controls to copy each managed participant's session ID or a shell-quoted resume command for the saved worktree.
- A documented agent MCP workflow for explicitly updating a clean official installation.

### Updated

- Installation and in-place upgrade instructions now cover fresh installs, the one-time 2.0.0 migration, and `a2a-link update` for later versions.
- The 2.1.0 issue workflow, remote test jobs, and CLI self-updater are included in this release.

### Verification

- Windows full suite: **747 passed, 5 skipped, 14 warnings**.
- Tailnet dashboard and `/usage` endpoint returned HTTP 200 on the configured Serve port.

## 2.1.0 — 2026-11-27

Update to the existing A2A-Link 2.0.0 GitHub repository. See [UPGRADING.md](UPGRADING.md) for in-place upgrade steps.

### Added

- Durable issue collaboration with separate managed Codex CLI and Claude Code sessions, isolated worktrees, proposal review, and verified integration rounds.
- A local operator dashboard for issue, runner, integration, and test-result delivery status.
- Background test jobs: an agent submits a trusted runner-profile name and ends its turn; Link resumes the same managed client session with results after the runner finishes.
- Local and tailnet runner APIs, runner-local platform-specific profiles, and automatic logical CPU count substitution for explicitly configured `pytest-xdist` profiles.
- User-scope MCP setup and in-place update instructions for Windows, macOS, and Linux.
- An explicit safe self-update command (`a2a-link update` / `agentctl update`) and matching MCP tool for agents.

### Compatibility and verification

- The v1 delegation MCP tools remain available.
- Existing `A2A_LINK_HOME` and `A2A_LINK_DB` paths remain in use. SQLite schema setup creates the new tables and applies additive column migrations.
- Final Windows verification, including the updater tests: **736 passed, 5 skipped, 14 warnings**. Live Codex CLI 0.171.0 and Claude Code 2.1.330 background-job acceptance passed across a balancer process restart; the runner applied `pytest -n 16` on the 16-logical-CPU host.
- Linux runner execution has not yet been verified on a real Linux host.
