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
| `context-packets/<task>.json` | object | The context packet the task's CURRENT round was cut with. Replaced per round. |
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
`depends_on`, `approved_paths`, `context_ids`, `validation`, `validation_cwd`,
`created_at`, `completed_at`.

Status is one of `pending`, `in_progress`, `blocked`, `completed`, `retired`,
`shipped_elsewhere`, `quarantined`. Only `completed` satisfies a dependency.

`context_ids` (ctx-04) names the context records above that the task says it was
written from. PROVENANCE, never authorization: `approved_paths` remains the whole
of what a round may write, and `tasks.effective_approved_paths` never reads this
field. Shape is checked (`tasks._ID_RE`, no duplicates) and existence is not —
the registry never resolves a record. Absent from every file written before it
existed, which loads as "cites no record", so `schema_version` stays 1 and there
is no migration step; a hand-edited `null` normalises to `[]` and a bare string
is refused rather than read as one id per character.

## Blocker record

`blockers/<id>.json`, one per blocker, id `blk-<task>-<NNN>` (zero padded, so
filename order is chronological within a task). `id`, `task_id`, `kind`
(`task_fatal` | `loop_fatal`), `code`, `question`, `detail`, `phase`,
`created_at`, `resolved_at`, `answer`, `recurrences`, `last_seen_at`,
`session_id`, `archived_reason`, `revised_refusals`, `lane_id`.

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

`lane_id` is WHICH LANE parked this (`_lane-0`, `_lane-1`, …) — descriptive
metadata, so an operator reading `blockers` after a fleet-fatal stop can tell
records from several lanes apart. Nothing branches on it: how far a park reaches
is decided from `code` (`blockers.fatal_scope`), and which lane has actually
stopped is read from the lanes' own state files. Empty on a record written
before it existed and on one recorded by a caller that names no lane; a bump
that names none keeps the lane the record already carries, because an absence of
information must not overwrite one.

Readers are tolerant of missing keys (each has a default) and INTOLERANT of
unreadable ones: a record that fails to decode raises rather than reading as
absent, because "no blocker" and "a blocker we cannot read" must not look alike.

### `planning_source_conflict` (ctx-06)

One blocker code is written by TASK GENERATION rather than by a park:
`planning_source_conflict`, recorded through `blockers.record_planning_conflict`
when two of the sources planning reads disagree about one subject. It is a
durable record and not a field in `state.json` precisely because this store
already survives a `task_fatal` park and the session reset that follows one.

Its fields carry the ordinary meanings with two conventions:

* `task_id` is always `(loop)` — generation runs BEFORE the task it would
  propose exists, so there is none to name.
* `phase` is `planning:<identity>`, where the identity digests the subject and
  both sides' (source, author, text). `BlockerStore.find_open` keys a record on
  `(task_id, code, phase)` and a bump REPLACES `question` and `detail`, so a
  constant phase would collapse two different disagreements into one record
  carrying only the later one's account of who disagreed — which is the entire
  content of the record. The digest is what makes one disagreement one record
  and the same disagreement seen twice a recurrence. The AUTHOR is in it because
  two sides of a WITHIN-TIER conflict carry the same source: three accepted
  tasks scoping one subject produce disagreements whose (source, text) pairs
  coincide whenever two of them word their scope alike.
* `kind` is `task_fatal`, not a new kind: `_KIND_RANK` promotes an unrecognised
  kind to `loop_fatal` rank, which would silently make a planning conflict the
  `primary_blocker` that `health`, `heartbeat` and `status` report the loop as
  stuck on.

Like `stranded_after_environment_fault`, it is recorded WITHOUT parking and
changes no task's status, so it is absent from `cli._RESOLUTION_PRECONDITIONS`
(whose keys must all be codes a park emitter can raise).

**A conflict with no record of this kind stops generation.** "Recorded, and
generation continues" is only a true sentence when the record exists, so
`audit/taskgen.generate_tasks` treats a conflict it could not write — no store
given, or the store raised — exactly as it treats one that bears on scope, and
says so in the proposal's `skipped`, which is what the audit report renders.

WHERE THE STORE COMES FROM ON THE SHIPPING PATH. `audit/executor.py:803` calls
`generate_tasks(reconciled, self._registry)` and passes nothing else, and that
signature is not ctx-06's to change, so the planning inputs travel ON the
registry: `cli._build_executor` attaches an `inbox.PlanningSources` — a
`BlockerStore` over `config.blockers_dir`, a lazy `inbox.TreeReader` over the
checkout, the operator's intake drafts as a source provider, and a note naming
the tier nothing reads — and `generate_tasks` reads it through
`inbox.planning_sources_of`. So a real audit records its conflicts and continues
past one that provably does not change scope; the stop above is the degenerate
case (a store that failed, or a caller that built an executor without the seam)
rather than the normal one. An attribute rather than a module-level global
deliberately: a global one caller installs is a global a failing test leaves
behind for every later test in its process.

## Audit finding (audit agent output)

`audit/findings.py`. Thirteen REQUIRED keys — `id`, `category`, `severity`,
`confidence`, `affected_files`, `symbols`, `evidence`, `impact`,
`proposed_action`, `dependencies`, `acceptance_criteria`, `validation_commands`,
`safe_to_parallelize` — and, since ctx-06, five OPTIONAL ones:
`current_behaviour`, `current_behaviour_citation`, `assumptions`,
`open_questions`, `context_ids`. An unknown key is still rejected; a missing
OPTIONAL key is not, so a report from an agent that has never heard of them is
still valid.

`current_behaviour` and `current_behaviour_citation` are a PAIR. Stating what
the code does today without saying where it was read is an uncited repository
claim: `audit/taskgen.generate_tasks` refuses it BY NAME and the finding becomes
no task. An agent that cannot cite something writes it under `assumptions`
instead, where it is carried as an assumption and never asserted as fact.

AND THE CITATION IS CHECKED. A `path:line` is free to type, so for a claim about
what the code does TODAY the cited location must be one `inbox.TreeReader` saw in
a real `git ls-files`; a path the checkout does not have, and a citation no
reader was available to check, are both refused (in different words — "nothing
was read" is not "not there"). The check is bounded to that reader and to claims
about current state: `proposed_action` and `impact` say what should become true
and may legitimately name a file this change will create.

`context_ids` are REFERENCES and assert nothing — the same rule
`Task.context_ids` states. They are rendered into the task so a reader can go
and check the records, and nothing believes one on their strength.

All four free-text additions count towards `MAX_FINDING_CHARS`, the measured
whole-finding budget; adding fields outside that sum would reopen the inflation
path the bound was set for behind new field names.

**HOW FAR THE OPTIONAL FIELDS TRAVEL, stated because the honest answer is "not
everywhere".** They reach `generate_tasks` on the direct path
(`parse_findings` → `reconcile` → `generate_tasks`) and are rendered into the
proposed task's description. They do NOT survive two hops, and neither file is
ctx-06's to change:

* `audit/reconcile.py:185` rebuilds a `Finding` field by field when it folds a
  duplicate, so a MERGED finding loses its `current_behaviour`, citation,
  assumptions, open questions and context ids. The direction is safe — a folded
  finding can lose a cited claim and cannot gain an uncited one, pinned by
  `test_audit_taskgen.py::test_a_merged_finding_still_asserts_nothing_uncited` —
  but the fresh-session context the fields exist to carry is gone.
* `audit/report.py` does not render them into the Markdown report, so the
  report → intake round trip (`inbox.parse_audit_findings` →
  `inbox.AuditFinding`) never sees them. That path is intake-01's promotion
  route, not this one; nothing on it claims to enforce the citation contract.

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

## Context packet

`context-packets/<task>.json`, one per task, written by
`context_packet.ContextPacketStore` at `AutoloopConfig.context_packets_dir` —
under the state directory, beside `executions/`, and NEVER inside the checkout:
`escape_detector` snapshots the observed checkout with an exclusion list that is
empty by measurement, so a packet written into the tree would be reported as
`checkout_escape_detected`.

`task_id`, `task_base_sha`, `worker_repo`, `digest`, `rendered_at`, `text`.

`text` is the packet, and `digest` covers exactly it — nothing else in the
envelope, and `rendered_at` deliberately sits OUTSIDE it, because two renders of
one repository state have to produce the same bytes. A file whose stored
`digest` does not cover its own `text` is UNREADABLE, not "close enough": it is
neither served to a prompt nor shown to a reviewer. Absent and unreadable both
read as "no packet" — unlike an execution record, which raises — because the
binding artifact is `TaskExecution.context_packet_sha256` next door, and the
review packet reports the difference rather than substituting anything.

REPLACED PER ROUND, never accumulated. `orchestrator._dispatch_task_postcommit`
renders one for every implement/revise round from the task's OWN worker
repository at `TaskExecution.task_base_sha` — below every path that can still
move that base, above the agent — so a revise round after
`_rebase_execution_if_stale` is cut from the base it now names. Each earlier
round's digest survives inside the review packet that was sent for it. An AUDIT
round renders none, and its record's digest stays empty.

THE AGENT IS HANDED THE RENDER, not this file. The same dispatch passes the
rendered section to the executor about to run
(`implement_executor.deliver_round_context_packet`, set immediately before the
executor call and cleared in a `finally`), because the loop's single
`TaskExecutor` is `cli._DispatchingExecutor` in production and forwards nothing
else. This file is the REVIEWER's copy, and the round trip through it is a
precondition: a round whose packet cannot be written and read back at the digest
its record carries does not start at all — no agent, no attempt charged, a
`context_packet_unavailable` task-fatal park naming the directory that failed
(task-fatal rather than loop-fatal only because every loop-fatal code must be
classified in `blockers.LANE_FATAL_CODES`/`FLEET_FATAL_CODES`, which ctx-05 could
not edit).
A digest in front of a reviewer for an artifact nobody can produce is evidence
of context that was never evidenced, which is worse than not running.

The packet is DATA. Nothing parses it, no gate reads it, and
`tasks.effective_approved_paths` is never handed a context reference. It is
rendered strictly after every stamp line, in the agent prompt and in the review
packet alike; see `docs/SECURITY.md` S33 for the two controls.

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

### Who WRITES a record (ctx-07)

`context_records.ContextRecordStore` — a directory, plus `repo_prefix`, the
repository path those files have. Both are the CALLER's to name and neither is
decided in the package: `repo_prefix` is what makes a record file's scope
askable at all, so a store that cannot state one (`""`) is a store in which every
record is outside every task's `approved_paths`, which is the fail-closed
answer an unwired loop gets. The DIRECTORY may not be inside the observed
checkout — the loop cannot commit what it writes there, and the next dispatch
refuses to start against the dirty tree it would leave — so the files live
outside it and the prefix says what they are called in it. `record_to_mapping` writes every field, including
the empty ones, and a record that does not read back through
`record_from_mapping` as itself is never written over one that loads today.

Exactly two fields ever move, and only at a task's completion
(`docs/AUTOLOOP.md`, "Reading a context closeout in the transcript"):

* `last_verified_commit` advances to the commit a completed round published,
  for a `feature` or `incident` record whose own `source_paths` that change
  altered and whose own file is inside that task's `approved_paths`. It says the
  paths were part of a change that passed post-commit validation and review at
  that commit — not that the invariant was re-proved;
* `superseded_by` is set by `context_records.superseded_record`, which returns a
  NEW record and leaves the old one's every other field alone. The old record
  stays in its own file; the successor gets its own. Deleting it deletes the
  reason it was made, which is the rule `docs/SECURITY.md` keeps for a resolved
  finding. A successor that is empty, padded, or the record itself is refused,
  because `context_resolver` reports a self-succession as dangling and leaves
  nothing to read instead.

Nothing else is rewritten, and nothing is deleted. A record the loop cannot
update in scope is named in a follow-up task rather than written anyway.

## Context explanation (ctx-08)

Not a stored artifact: `python3 -B -m autoloop context explain --task <id>`
renders it on demand and writes nothing. `context_packet.explanation_lines` is a
pure function of what it is handed — a `PacketRender` (or `None`), the task, the
execution record and the stored packet — and it holds ONE recorded value up as
the answer rather than deciding one itself.

**The anchor is `TaskExecution.context_packet_sha256`**, written by the loop from
its own render at dispatch. `context_packet.provenance_verdict(render, execution,
stored)` compares against it and returns exactly one of
`PROVENANCE_AS_DISPATCHED` (the re-render reproduces it — the sections ARE the
round's), `PROVENANCE_RECORDED_ONLY` (the stored packet hashes to it and the
re-render does not — the stored bytes are printed first, as the answer, and the
re-resolution follows as a labelled comparison) or `PROVENANCE_UNVERIFIED`
(neither, or the record carries no digest at all — an empty digest matches
nothing, an empty stored one included).

`PacketRender`: `packet`, `resolution` (`None` when the base could not be read),
`resolution_error`, `entries` (the tree listing the object ids came from,
`None` when it could not be listed at all — never `{}`), `entries_error`,
`index_wired`, `records_line`, `rev`, `tree`. The render itself is `None` when no
re-resolution was possible at all — a worker repository that has moved — and
`re_render_error` then carries the stated reason.

The rendered sections are `provenance` (the verdict, and what proves it),
`digest` (the recorded one, then the stored file's and the re-rendered one, each
compared against that anchor), `as recorded at dispatch` (the round's own packet
verbatim, printed rather than parsed, whenever the sections are not proven to be
those bytes), `selected records` (the packet's own
`selection_block`, called and not re-spelled), `rejected records`
(`context_resolver.REJECTED_CATEGORIES`), `stale or unverified records`
(`STALENESS_CATEGORIES` — both halves of the tri-state that are not `fresh`),
`contradictory records`, `other findings` (the partition remainder, so a
category no reader knows yet is printed rather than filtered away) and `bounds`
(what was not printed — for the recorded packet AND the re-render, which are two
artifacts and are accounted for separately). See `docs/AUTOLOOP.md`, "Asking why
a task got the context it got".
