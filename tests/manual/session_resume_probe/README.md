# Session resume probe

Manual check that a Link-managed Codex CLI or Claude Code CLI session keeps the
same session ID across process exit and resume. The command prints a sanitized
JSON report; save it to a location of your choice if you need to keep evidence.

`probe.py` starts real CLIs, which use your local login and model quota. It runs
only with `--run` and pytest never runs it. `test_probe_report.py` checks the
parser and report offline:

```
python -m pytest tests/manual/session_resume_probe -q
```

## Run

From the repository root, on the machine whose clients are under test:

```
python -m tests.manual.session_resume_probe.probe --provider codex --run
python -m tests.manual.session_resume_probe.probe --provider claude --run
```

Options: `--delay N` sets the gap between turn end and resume (default 30 s),
`--timeout N` sets the per-turn timeout, and `--unknown-id-check` additionally
resumes a random ID to see whether the CLI rejects it or silently starts a new
session. That last check can leave a stray session behind if the CLI substitutes
one, so it is opt-in.

Argv comes from `mcp_server.issue_adapters` (`CodexAdapter`, `ClaudeAdapter`),
the same commands Link uses for managed sessions:

| Step | Codex CLI | Claude Code CLI |
|---|---|---|
| start | `codex exec --json -s workspace-write -c mcp_servers.a2a-link.enabled=false <prompt>` | `claude -p <prompt> --output-format stream-json --verbose --permission-mode acceptEdits --strict-mcp-config` |
| handle source | first JSONL event `{"type":"thread.started","thread_id":…}` | first event `{"type":"system","subtype":"init","session_id":…}` |
| resume | `codex exec resume <thread_id> --json -c sandbox_mode="workspace-write" -c mcp_servers.a2a-link.enabled=false <prompt>` | `claude -p <prompt> … --resume <session_id>` |
| ID on resume | `thread.started.thread_id` of the resume process | `system/init.session_id` and `result.session_id` |

Each turn runs in a fresh temporary git repository that is deleted afterwards.
The report is JSON with versions, OS, return codes, booleans and ID shape
(`uuid`/`opaque`/`missing`). IDs, prompts, replies, stderr and paths are never
printed. Exit code 0 means every check passed.

## Checklist

Fill one row set per provider in the evidence record. All boxes must come from
an actual run on the installed client version, not from documentation.

- [ ] **Versions recorded:** `cli_version` and `os` from the report.
- [ ] **Session registration:** the start process emitted exactly one start event
      carrying a session ID, and Link read it from that event without the agent
      supplying it (`session_registration_from_start_event`).
- [ ] **Assistant turn end:** the start process exited 0 with a completed turn
      (`assistant_turn_end_observed`), so the session had no live process during
      the delay.
- [ ] **Completion delay:** the resume ran `--delay` seconds after the first
      process exited (`completion_delay_seconds`).
- [ ] **Same-ID continuation:** the resume process reported exactly the stored ID
      and no other (`completion_resume_same_id`) and remembered the code word from
      the first turn (`completion_turn_has_prior_context`).
- [ ] **Newer queued message:** a second, later resume with a new message kept
      the same ID (`newer_message_resume_same_id`) and context
      (`newer_message_turn_has_prior_context`). The probe runs these one after
      another; ordering and queuing while a turn is active belong to Task 3.
- [ ] **Unknown ID (optional):** with `--unknown-id-check`, record whether the CLI
      rejected the ID (`rejected`) or created a new session
      (`substituted_new_session`). If it substitutes, the adapter must compare
      the reported ID with the stored one and fail closed.
- [ ] **No leak:** the pasted report contains no session ID, transcript,
      credential or local path.

A failed check means the provider is not supported for same-session resume:
record it as `FAIL`, keep the result pending, and do not fall back to a new
session.
