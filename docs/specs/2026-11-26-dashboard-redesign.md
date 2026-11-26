# A2A-Link Dashboard Redesign

## Goal

Redesign the local balancer dashboard as an English-language dark operator console that makes agent activity, capacity, and task details easy to scan while work is running.

## Current state

- `GET /dashboard` serves `balancer/static/dashboard.html`.
- `GET /events` streams tasks, usage, and incidents as server-sent events.
- Selecting a task fetches its detail and log through `GET /tasks/{task_id}` and `GET /tasks/{task_id}/log`.
- The page already renders task, issue, usage, and incident data, but uses a basic document layout, Polish labels, and an inline task detail section below the table.

## Proposed experience

- Use a restrained dark palette, clear typography, accessible contrast, compact spacing, and a responsive layout without adding a frontend framework or build step.
- Add a top header with the A2A-Link name, a short “Local agent operations” subtitle, and a live connection indicator driven by the SSE connection state.
- Show concise summary cards for running tasks, completed tasks, and tasks requiring attention, derived from the existing task snapshot.
- Present model usage as separate compact cards with a labeled percentage bar and reset time when available.
- Make the task table the main area, with English labels for task ID, agent, status, duration, task, and error. Keep live durations and row selection.
- Open task details in a side panel on wide screens and below the selected task on narrow screens. Include the prompt, current status and timestamps, live log, and result or error. Keep the existing two-second detail refresh while a task is running.
- Preserve the v2 issue workspace from the existing dashboard. Present issue goal, round/base, acceptance criteria, participant handles/readiness/blockers, message profiles/context, proposals, decisions, generation handoffs, and integration/test status in collapsible cards with English labels.
- Show incidents in a secondary section with readable timestamps and clear severity treatment.
- Include useful loading, empty, disconnected, and request-error states so a fresh clone with no task history still looks intentional.

## Constraints

- Keep all visible dashboard copy in English.
- Keep the page local-first and served from `127.0.0.1`; do not add external assets, remote requests, tracking, or new runtime services.
- Reuse the existing balancer API and event payload, including its `issues` collection; do not remove issue data or change MCP behavior.
- Escape all server-provided text before inserting it into HTML, and preserve text-only rendering for logs and task details.

## Acceptance

1. The dashboard presents a polished dark operator layout at desktop and narrow viewport widths.
2. Every static UI label, empty state, connection state, and error message is in English.
3. Live task rows, duration updates, usage bars, incident feed, task selection, logs, result/error, automatic detail refresh, and all existing issue snapshot fields remain visible and work with the existing endpoints.
4. With no data, the page shows clear empty states; when the event stream disconnects or a detail request fails, the page communicates that state without losing the rest of the dashboard.
5. The dashboard remains a single packaged HTML asset served by `GET /dashboard`; no frontend build step or new dependency is introduced.

## Out of scope

- New REST endpoints or schema changes.
- New issue-round actions; the page remains read-only.
- User authentication, remote hosting, or public network binding.
- Changing the default browser theme or persisting user preferences.
