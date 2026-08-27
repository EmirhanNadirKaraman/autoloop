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

## Transcript event

`{"ts", "type", "iteration", "request_id", "data"}`. An operation that records
its own elapsed time puts it under `data.duration_seconds`; a record without one
is not an error, it predates the measurement.
