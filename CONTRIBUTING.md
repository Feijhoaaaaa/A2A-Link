# Contributing to A2A-Link

Thanks for helping improve A2A-Link. This guide explains how to set up a local development environment and make a change that fits the project.

## Project map

- `balancer/` — FastAPI application, SQLite persistence, routing, dashboard, and `agentctl` CLI.
- `balancer/static/dashboard.html` — the self-contained dashboard. It uses the existing balancer HTTP and server-sent event endpoints; no frontend build step is required.
- `mcp_server/` — MCP tools, managed agent processes, issue coordination, worktree isolation, and terminal policy enforcement.
- `config/` — safe-to-publish example policy and agent-profile configuration. Personal copies belong in `~/.a2a_link`, not in Git.
- `tests/` — automated unit and integration coverage for the balancer and MCP server.
- `docs/specs/` — design decisions and client verification records.
- `docs/superpowers/plans/` — implementation plans and their acceptance checklists.

## Development setup

Requirements: Python 3.11+, Git, and (for live delegation) the agent CLI you intend to use, authenticated with your own account.

```bash
git clone https://github.com/Feijhoaaaaa/A2A-Link.git
cd A2A-Link
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[test]"
```

On Windows PowerShell, create the environment with `py -3.11 -m venv .venv` and replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

## Implementation workflow

1. Read the relevant design spec, implementation plan, and nearby code before editing.
2. Record the current branch and `git status --short`; never overwrite or include unrelated user changes.
3. For behavior changes, add or update a focused test that demonstrates the expected behavior, then run it and the full suite:

   ```bash
   .venv/bin/python -m pytest -q
   ```

   On Windows, use `.venv\Scripts\python.exe -m pytest -q`.

4. Keep changes small and use existing public interfaces unless the spec calls for an API change. For dashboard work, preserve the current API payload and escape all server-provided text.
5. For changes to the MCP server, restart the MCP client or open a fresh session before doing a live check; the client keeps its server process for the session.
6. Review `git diff --check`, the full diff, and `git status --short`. Do not commit databases, logs, virtual environments, personal profiles, credentials, or terminal policy files.
7. Update the relevant docs and plan checkboxes only when there is evidence the behavior is complete. Include commands run and their results in the pull request.

## Security boundaries

- Keep the balancer bound to localhost. Public hosting is out of scope unless authentication and access controls are designed and reviewed.
- Never commit personal `terminal-policy.json`, terminal audit logs, agent profile overrides, database files, or API credentials.
- The MCP terminal allowlist is defense in depth, not a replacement for the agent CLI sandbox or operating-system permissions.
- Avoid logging prompts, command output, credentials, or other private project data unless the feature explicitly requires it and the data remains local.

## Pull requests

Describe the user-visible behavior, link the related spec or issue, list verification performed, and call out any remaining limitation. Keep a pull request focused on one coherent change.
