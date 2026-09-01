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
`session_id`, `archived_reason`, `refusal_fingerprint`.

`task_id` is `(loop)` for a blocker tied to no registry task; task ids cannot
contain parentheses, so the two never collide. `answer` means an operator
responded; `archived_reason` means the loop closed the record itself and is
never written into `answer`.

`recurrences` counts how many times the same (task, code, phase) has re-parked,
and autonomous recovery meters its per-code budget on the sum of it across every
OPEN record for a (task, code) — deliberately blind to phase, so a fault that
migrates one phase along keeps spending one allowance.

`refusal_fingerprint` is a digest of one refusal's (code, question, detail),
written only for the codes autonomous mode answers with a `revise` and read only
by the repeat guard. Empty on every record written before it existed and on
every other code, which reads as "no identity to compare" — never as a match.

Readers are tolerant of missing keys (each has a default) and INTOLERANT of
unreadable ones: a record that fails to decode raises rather than reading as
absent, because "no blocker" and "a blocker we cannot read" must not look alike.

## Transcript event

`{"ts", "type", "iteration", "request_id", "data"}`. An operation that records
its own elapsed time puts it under `data.duration_seconds`; a record without one
is not an error, it predates the measurement.
