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
| `wanted_decisions.json` | object | `{verb: count}` — the verbs reviewers said they WOULD have used, `none` included. Evidence for a human; enforces nothing, so an unreadable file is read as empty and rewritten. |
| `LOCK` | text | One holder per state dir. Never stolen. |
| `fleet_throttle.json` | object | The fleet's ONE rate-limit episode (conc-11). Written only at `[concurrency] lanes > 1`. See below. |

## Fleet throttle record

`fleet_throttle.json`, beside `LOCK` and for the lock's own reason: one state
directory is one account's fleet, so "this account is throttled" is a fact about
the directory rather than about a lane. N lanes draw on ONE ChatGPT allowance,
and per-lane state files would otherwise turn one limit into N independent
back-offs.

`backoffs` (int ≥ 1) — the fleet's CONSECUTIVE-episode count, what
`policy.max_rate_limit_backoffs` is checked against and what
`_rate_limit_delay` doubles from. Episodes, never observations.
`retry_not_before` (ISO 8601, UTC) — the one shared, un-jittered deadline; each
lane adds `k/lanes` of `[concurrency] rate_limit_release_jitter_seconds` on top
before re-probing. `opened_at`, `opened_by` (lane id) — who started the episode.
`observations` (int ≥ 1) — how many lanes have met THIS episode; `4` beside
`backoffs = 1` is four lanes throttled by one limit producing one episode.
`episode_id` (string) — names this episode: minted where one opens, carried
unchanged by every lane that joins it, never reused, and mirrored into the
observing lane's `state.json` as `fleet_throttle_episode`. `updated_at`.

Written atomically (temp file with the writer's pid in its name, then
`os.replace`) and mutated only under `tasks.task_file_mutex`, so the
read-decide-write of joining an episode cannot race. Ending an episode is a
COMPARE-and-clear under that same mutex: a lane whose step completes removes the
record only while its `episode_id` still matches, because between its retry and
its clear another lane can have opened the next episode, and deleting that would
erase a live deadline and an escalated counter. An `episode_id` of `""` — a
record written by hand, or a lane that never observed one — clears nothing.
**Absent at `lanes = 1`, and never created there.** A record that cannot be read
is refused rather than read as "no throttle": admission holds and the next
throttled lane parks naming the file.

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

## Execution record retirement

`executions/<task>.json` is meant to be retired WITH the work it describes:
MOVED, never deleted, into `executions/archive/<task>-<label>.json` by
`worktask.retire_execution`, which `release` and `discard` both call. Readers of
the live records glob `executions/*.json`, which does not recurse, so an
archived record leaves the merge window's view while staying recoverable.

A record can still outlive its task — a retirement that failed halfway, an
operator edit, or (2026-08-27) a history rewrite leaving records for ids the
registry no longer holds. `cli._merge_window_blockers` EXCLUDES such a record
from the merge window when three facts all hold: the registry has no task by
that id, no worker repo exists for it at either its recorded `worktree_path` or
`workers_root/<task_id>`, and `published_sha` is unset. The exclusion is
reported as a note naming the record, the reason and the remedy — never applied
silently. If any of the three cannot be established the record still holds the
window shut, exactly as a live, worker-backed or published one does.

Nothing archives an orphaned record automatically. `release` and `discard` both
require a task id the registry knows, so retiring one is still a move into
`executions/archive/` by hand.

## Context record

One JSON object per file, `*.json`, in a directory the CALLER names — ctx-03
fixes the SHAPE and deliberately not the location: `context_resolver.
resolve_context` is pure given its inputs the way `context.build_context` is,
and wiring a directory (and `Task.context_ids`) into the loop is ctx-04's.
Read by `context_records.load_records`, indexed by `context_index.load_index`.

`id` (required, unique across the directory, compared verbatim — a padded value
is refused rather than stripped), `kind` (required; `decision` | `feature` |
`incident` | `lesson`, and no fifth), `title`, `invariant`, `source_paths`,
`related_ids`, `last_verified_commit`, `superseded_by`. Unknown keys are refused
at parse time, in `load_config`'s style: a typo'd `source_path` would otherwise
load as a record asserting nothing about no files, which can never be found
stale, missing or contradictory.

A record is a claim about `source_paths` AT `last_verified_commit`. That pairing
is what makes staleness a question about TREES: the resolver compares
`tree_of(last_verified_commit)` with the tree of the checkout it was resolved
against (`GitGateway.changed_paths`, one `diff-tree` per DISTINCT commit) and
marks the record stale only when its own paths are among the changed ones. HEAD
advancing over other files leaves it fresh, and a change made and then reverted
leaves it fresh too — which is the correct answer, and the reason no history
walk is used. Nothing here widens `policy._ALLOWED_GIT`.

`superseded_by` NON-EMPTY IS THE WHOLE ASSERTION: such a record is never
returned as active and is never expanded through, whether or not the successor
id resolves — an unresolvable successor is reported as its own finding and does
not restore the record. `related_ids` is the ONLY edge the resolver follows, and
it is directed as written. `invariant` is what two active records can disagree
about over one source path; a conflict is RECORDED, with both sides, and no
winner is picked.

Two files declaring one `id` is a DUPLICATE: neither is indexed, under any
lookup, and the id is reported naming both files. A file that will not parse is
reported by name rather than dropped. Staleness is a TRI-STATE — `fresh`,
`stale`, `unknown` — because a record whose commit no longer resolves has not
been shown to be fine, and reporting it as fresh is the one answer that would
make the alarm silent.
