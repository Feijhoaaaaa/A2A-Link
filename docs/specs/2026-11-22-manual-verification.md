# Manual verification guide

Use this checklist for a local smoke test after installing A2A-Link. It contains expected behavior, not recorded results from a particular machine or user session.

## Start the balancer

From the repository root, activate the project environment and run:

```powershell
.\.venv\Scripts\python.exe -m balancer.app
```

Open <http://127.0.0.1:8000/dashboard>. The dashboard should show the balancer connection as online and display task, usage, issue, and incident data when those records exist.

The balancer listens on localhost by default. Keep its database and personal policy/profile configuration outside the repository.

## Run automated checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Check the MCP server

Register the MCP server in the client you use, then restart that client so it starts the configured server process. Use the example terminal policy only after replacing its allowed roots and audit log with absolute paths that exist on your machine.

- `describe_terminal_policy` should report the configured allowlist.
- A command allowed by the policy should run and produce an audit record.
- A command outside the allowlist should be denied without running.
- A request requiring approval should fail closed when approval is unavailable or declined.

## Check agent delegation

With the relevant Claude Code or Codex CLI installed and authenticated, delegate a small harmless task. Confirm that the caller receives a task identifier or an in-progress response, that the task reaches a terminal state, and that the dashboard displays its status and logs. Run one task for each CLI configured on your machine.

For worktree-isolated delegation, verify the delegated changes are in the task worktree and the caller's checkout remains unchanged until the user integrates them.

## Check issue collaboration

Use the MCP issue tools to open an issue with a short goal and acceptance criteria. Confirm that the dashboard shows the issue, its participants, and its progress. Ask each managed participant to make a small independent change, then inspect the resulting worktrees before accepting or integrating any proposal.

Restart the balancer while an issue is idle, then reopen the dashboard. Persisted issue state should remain available from the configured SQLite database. For managed CLI continuation and restart behavior, use the automated supervisor tests and the issue API contract implemented by the project.

## Report a problem

Include the command run, expected behavior, observed behavior, relevant test output, and client/CLI versions. Remove prompts, credentials, local usernames, session identifiers, and private project data from reports and logs before sharing them.
