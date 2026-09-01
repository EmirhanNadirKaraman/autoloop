# SECURITY.md

Living security tracker: open findings, resolved findings, and the controls
relied on. Read it before changing anything that touches git operations,
subprocess invocation, the publisher, or an agent's write scope.

Every finding carries four things, or it rots into an untested claim:
`file:line`, a severity, a one-line verification check, and a suggested fix.

## Verified strengths

- Git operations run through `GitGateway` behind a policy; force pushes and
  destructive commands are denied rather than merely discouraged.
- `protected_branches` refuses direct pushes to the base branch.
- The reviewer gets no repository access: its working directory is outside the
  checkout and its prompt is self-contained.
- The escape detector brackets every write-capable agent call.

## Open findings

_None recorded yet in this repository._

## Change notes — append ONE new line at the END of this file

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-27 | split | extracted from language-app with git filter-repo, 414 commits of history preserved; this tracker seeded here. |
| 2026-08-31 | brw-19c | `autoloop/cli.py:_precondition_browser` — a name-whitelist recheck is disarmed by deleting the checks it names, and nothing fails while it happens. Severity: medium; a blocker recording an unresolved ENVIRONMENT fault becomes resolvable by answer text alone, which is Autoloop M1 finding #7's exact shape. Verify: answering a `login_expired` blocker must be REFUSED while the configured provider cannot be opened. Fixed here — the whitelist is `_BROWSER_PRECONDITION_CHECKS`, led by `primary_live`, which `run_doctor` still emits; `cdp`/`playwright` are retained but dead. |
| 2026-08-31 | brw-19d | `autoloop/cli.py:3365` `_precondition_transport_live` — a name-filter recheck that reads ABSENT evidence as a pass. Severity: medium; `login_expired`/`submission_ambiguous` become resolvable by answer text alone once doctor stops emitting the filtered rows. Verify: stub `doctor.run_doctor` to return no `primary_live` and no `provider` row, then answer such a blocker — it must be REFUSED and stay open. Fixed: required rows must be PRESENT and `ok`, a sweep that raises refuses, and `autoloop/cli.py:3417` `_precondition_git_health` takes `git_failure_budget_exhausted` off transport evidence entirely. |
| 2026-08-31 | notify-01 | `autoloop/notify.py:397` `_resolve_credentials` — NEW OUTBOUND SURFACE: the loop authenticates to an SMTP server and mails its status from inside `heartbeat.publish`. Severity: medium — falling back to an unauthenticated send would put status text wherever that port answers, and the credential is the operator's mail account. Verify: with `[notify].enabled = true` and the variable named by `password_env` unset or empty, or `password_file` missing or blank, the send must be REFUSED with the reason on stderr and nothing sent. Pinned by `test_notify.py`'s seven no-password shapes. |
| 2026-08-31 | notify-01 | Fix for the surface above: no config key holds the password (`config.py:_load_notify_section` refuses `notify.password`/`smtp_password` by name, names rotation, never echoes the value), exactly one of `password_env`/`password_file` is required when enabled, `tls = "none"` is refused so it is never sent in clear, and every rendered or logged string passes one choke point, `autoloop/notify.py:199` `_safe`, which replaces the password with `***` and rewrites the operator's home to `~`. NOT enforced, check by hand: `password_file` outside the checkout, `chmod 600`. |
| 2026-09-01 | wanted-01 | `autoloop/policy.py:468` `_check_wanted_decision` — a NEW field required on every directive, and why it is not a new authority surface. Severity: informational. The gate tests PRESENCE only: it never compares the value to `Decision`, never restricts it to a vocabulary, and `orchestrator._dispatch` still branches on `decision` alone — so a reviewer writing `wanted_decision: "push"` gets it COUNTED, not executed, leaving finding #2's circular-ownership bound exactly where it was. Verify: a `stop` naming any real verb leaves the candidate, the registry and the worker repo untouched. No fix needed; pinned by `test_wanted_decision.py`. |
| 2026-09-01 | prov-01 | `autoloop/codex/conversation.py:372` `submit` — the verdict was read from the WHOLE of `codex exec` stdout, which carries the ECHOED PROMPT under a `user` role marker. Severity: medium. The prompt carries the response contract and, in the captured shape, an example directive, so text the loop SENT could be read back as the approval that authorizes a commit or a push — `quota.py`'s echo hazard, on the other stream. Verify: stdout whose `user` segment holds a complete `commit_and_push` and whose `codex` segment holds a `push` must yield `push`. Fixed: `codex/reply.py` isolates the `codex` segment; pinned by `test_codex_stdout_verdict.py`. |
| 2026-09-01 | prov-01 | `autoloop/codex/reply.py:277` `_turn_markers` — isolating "the segment after the `codex` marker" is not on its own enough: this loop maintains itself, so its packets QUOTE codex transcripts and a marker plus a complete directive arrive inside the echoed prompt. Severity: medium; an echoed example carries this round's real `request_id` and `head_sha`, so `orchestrator`'s stamp gates would accept it. Verify: stdout whose only `codex` marker sits inside the echoed prompt must be REJECTED, never parsed. Fixed: markers must be at column 0 AND at a turn boundary, and `reply.py:375` `_anchor` skips a verbatim echo outright. |
| 2026-09-01 | prov-01 | `autoloop/codex/reply.py:517` `isolate_reply` — the bound above holds only while the echo is VERBATIM: a build that re-wraps its echo defeats the exact match, leaving the packet's own quoted directive as the single segment. Severity: medium; it carries this round's real `request_id` and `head_sha`, so the stamp gates accept it and an approval nobody made authorizes a push. Verify: a reflowed echo quoting a flush-left same-round directive, with no reviewer message, must be REJECTED. Fixed: `reply.py:474` `_accept` refuses any candidate the prompt contains, literally or on `quota._squeeze`'s basis. |
