# SCHEMA.md

The shapes of the files the loop reads and writes. These are contracts: a
process that dies mid-write must leave something the next process can still
read, so every write is atomic and every reader is tolerant.

## State directory

Configured by `[paths].state_dir`. Not inside the checkout, deliberately — the
loop rewrites the checkout, and its own memory must not be part of what it
rewrites.

| File | Shape | Notes |
|---|---|---|
| `state.json` | object | One `phase`, one `current_task`, the pending request. |
| `tasks.json` | object | The task registry: id, status, priority, `depends_on`, `approved_paths`. |
| `transcript.jsonl` | one JSON object per line | Append-only event log. A partial final line is expected and tolerated. |
| `executions/<task>.json` | object | Per-task execution record: branch, base sha, candidate sha, review round, attempt ledger. |
| `blockers/` | one file per blocker | Open and resolved blockers, with the code that raised them. |
| `pending_upgrade.json` | object | A merge that changed loop code, and whether the handoff happened. |
| `LOCK` | text | One holder per state dir. Never stolen. |

## Task

`id`, `title`, `description`, `status`, `priority` (ascending; 1 outranks 2),
`depends_on`, `approved_paths`, `validation`, `validation_cwd`, `created_at`,
`completed_at`.

Status is one of `pending`, `in_progress`, `blocked`, `completed`, `retired`,
`shipped_elsewhere`, `quarantined`. Only `completed` satisfies a dependency.

## Blocker record

`blockers/<id>.json`, one per blocker, id `blk-<task>-<NNN>` (zero padded, so
filename order is chronological within a task). `id`, `task_id`, `kind`
(`task_fatal` | `loop_fatal`), `code`, `question`, `detail`, `phase`,
`created_at`, `resolved_at`, `answer`, `recurrences`, `last_seen_at`,
`session_id`, `archived_reason`, `revised_refusals`.

`task_id` is `(loop)` for a blocker tied to no registry task; task ids cannot
contain parentheses, so the two never collide. `answer` means an operator
responded; `archived_reason` means the loop closed the record itself and is
never written into `answer`.

`recurrences` counts how many times the same (task, code, phase) has re-parked,
and autonomous recovery meters its per-code budget on the sum of it across every
OPEN record for a (task, code) — deliberately blind to phase, so a fault that
migrates one phase along keeps spending one allowance.

`revised_refusals` is a list of refusal identities — each a digest of one
refusal's (code, question, detail) — that autonomous mode has already answered
with a self-issued `revise`. It records ACTIONS, not occurrences: an entry is
appended at the moment a revise is issued, never on a park that merely happened.
The repeat guard meters one revise per identity and counts across CLOSED records
too, so answering or archiving a blocker cannot refund an allowance. Empty on
every record written before it existed and on every code autonomous mode does not
answer with a revise. A value that is not a list of strings is treated as a
corrupt record and RAISES, because "we cannot read the meter" must not read as
"nothing was spent".

Readers are tolerant of missing keys (each has a default) and INTOLERANT of
unreadable ones: a record that fails to decode raises rather than reading as
absent, because "no blocker" and "a blocker we cannot read" must not look alike.

## Audit intake ledger

`<intake_dir>/audit_intake.json` — one object, keyed by the QUALIFIED finding id
(`db_migrations:db-01`) exactly as `inbox.parse_audit_findings` reads it out of a
rendered report. Beside the drafts and `declined.json`, outside the checkout: the
escape detector snapshots the checkout, and `TaskInbox.drain` would eat a `*.json`
written into the inbox directory itself.

Each value: `outcome` (`promoted` | `already_done` | `declined`), `fingerprint`,
`title`, `source`, `detail`, `task_id`, `evidence`, `recorded_at`.

`fingerprint` is a digest of the finding's (qualified id, title) — the evidence
the decision was made about. A record only applies while it matches, so a
re-worded finding reopens rather than staying silently closed, and a finding
nobody touched stays closed across every later run.

`outcome` is a CLOSED vocabulary: a value outside those three is read as no
record at all, so a hand-edited or future ledger leaves its finding OUTSTANDING
rather than making it vanish from the dashboard under a word nothing understands.

An ABSENT file means nothing has been recorded yet. An UNREADABLE one is a
different fact and never collapses into that one: readers filter nothing and say
so, and `record_audit_outcome` refuses to write — the file is every decision
already made, and rewriting it from `{}` to record one more would destroy them.

## Transcript event

`{"ts", "type", "iteration", "request_id", "data"}`. An operation that records
its own elapsed time puts it under `data.duration_seconds`; a record without one
is not an error, it predates the measurement.
