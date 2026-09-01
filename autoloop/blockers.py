"""Persistent operator-facing blocker records.

Continuous mode (`cli.py`'s `run --continuous`) is multi-track: when a park
site in `orchestrator.py` is classified `task_fatal` (see `_to_needs_user`'s
`kind` parameter and the classification table in its module docstring),
only the ONE task at fault is set aside — the loop keeps working whatever
else is READY (`tasks.TaskRegistry.block`/`unblock`). A `loop_fatal` park
still stops the loop outright, exactly as every park did before this module
existed.

Either way — task_fatal or loop_fatal — the operator-facing question that
would have been shown at the park site is durably persisted here, one JSON
file per blocker, so it survives whatever the loop does next (clearing the
session state for a task_fatal park, or the loop simply exiting for a
loop_fatal one) and can be listed/answered later:

    python -m autoloop blockers            # list open ones (--all for resolved too)
    python -m autoloop answer <id> "..."   # resolve + unblock the task if task_fatal

When more than one is open at once, WHICH of them is "the" blocker is a
decision this module makes for everyone: `primary_sort_key` (severity, then
recency, then id) and the `BlockerStore.primary_blocker` /
`open_blockers_by_severity` pair beside `open_blockers`. Every caller that
needs a single blocker reads it from here; a second sort anywhere else is
the bug that rule exists to prevent.

Same crash-safety rule as every other store in this package
(`worktask.TaskExecutionStore`, `tasks.TaskStore`): a corrupt record RAISES
(`StateCorruptError`) rather than silently reading as absent. Reading a
blocker as "not there" when it actually failed to decode would let the
operator believe a problem was resolved (or never existed) when it did not —
exactly backwards for a record whose entire purpose is not losing track of
open questions.

Autonomous recovery (halt-02, 2026-08-25) lives here too — `HARD_HALT_CODES`,
`AUTONOMOUS_RECOVERIES` and `autonomous_recovery`. It is a TABLE, in this
module rather than in `orchestrator.py`, for two reasons. The table is data
about blocker codes and this is the blocker-code module; and the orchestrator's
source is read as evidence by `test_transport_recovery.py`'s retired-code walk,
so a table of code strings there would be indistinguishable from an emitter.
`orchestrator._to_needs_user` is the only caller.

halt-03 (2026-08-25) adds the SECOND half of that table,
`STALE_RECORD_RECOVERIES`: six codes whose fault is not a transport fault at
all but a RECORD the loop is still holding that no longer describes anything —
a base pinned behind the branch head, an approval binding naming a candidate
that moved, a packet that can never bind the review it is standing in front of,
a session pointer at an audit nobody minted. Their remedy is one action,
`RECOVER_BY_REBUILDING_AT_HEAD`: archive the stale record, rebuild at the
current head, re-dispatch. That is
exactly what an operator does by hand today, which is why the median park on
`task_base_behind_head` was 0.54h across 15 parks — seeing it IS deciding it.
The two dicts are merged into `AUTONOMOUS_RECOVERIES`, which stays the ONE
table `autonomous_recovery` reads, so the hard-halt refusal cannot be bypassed
by looking a code up in the newer half directly.

halt-01 (2026-08-26) adds the THIRD half, `EXHAUSTED_BUDGET_RECOVERIES`: seven
codes whose fault is neither a transport fault nor a stale record but a BUDGET
or CEILING that has run out. A budget means "stop churning on THIS task", not
"stop everything", and six of the seven stop everything today. Their recovery
path is empty by construction — the remedy is an operator raising a limit — so
every entry is `RECOVER_UNAVAILABLE` with a budget of 0 and the set-aside fires
on the first occurrence. `SESSION_CEILING_CODES` is the deliberate exclusion
beside them, and is refused exactly like a hard halt.

halt-04 (2026-09-01) adds the FOURTH half, `REFUSED_WORK_RECOVERIES`: seven
codes that are none of the three above. Nothing transient failed, no record went
stale and no counter ran out — the loop REFUSED the work a round produced, and
then parked so a human could relay that refusal to the agent by hand. A refusal
is feedback, so every one of them is instead returned to the agent as feedback:
`RECOVER_BY_REVISING` re-dispatches the task with the refusal's own text as the
`revise` feedback.

THE REPEAT GUARD IS THE HARD PART, not the resend, and it is metered on the
REFUSAL rather than on its code. `refusal_identity` digests one refusal's
(code, question, detail); `BlockerStore.note_refusal_revise` spends that
identity's single allowance at the moment a revise is issued, and
`BlockerStore.refusal_revises` counts it back across every record on disk,
closed ones included. So the SAME complaint arriving again sets the task aside,
while a genuinely different complaint under the same code is feedback the agent
has never been given and gets its own revise. That is why the meter is not
keyed on (task, code): two occurrences of `post_commit_verification_failed` are
routinely two different faults, and parking the second because the first spent
the code's allowance would park work that had an answer.

WHAT STOPS A CODE THAT CHURNS ITS TEXT, since the identity meter by itself does
not: every self-issued revise is dispatched through the ordinary path, so it
costs one of `MAX_TASK_ATTEMPTS` and one of `policy.max_review_rounds` exactly
as a reviewer's does — and both ceilings are themselves set-aside codes in
`EXHAUSTED_BUDGET_RECOVERIES`. A task whose refusals never repeat therefore
ends at `attempt_count_ceiling`, quarantined, rather than looping.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import StateCorruptError, StateError
from .state import utcnow_iso

#: Sentinel `task_id` for a blocker not tied to any specific registry task
#: (e.g. a login expiry, or an iteration-budget exhaustion mid corrective
#: re-prompt — the loop-level failures every park used to be before
#: task_fatal existed). Never a real task id: `tasks._ID_RE` forbids
#: parentheses, so this can never collide with one.
NO_TASK = "(loop)"

#: `code` for a task the loop could NOT return to scheduling after a round the
#: environment destroyed (strand-01, 2026-08-24). Written by
#: `orchestrator.Orchestrator._reconcile_stranded_tasks` when the record is
#: outside the narrow safe shape — it holds a candidate, a reviewer has already
#: seen a round, its attempt budget is spent, or it cannot be read at all.
#:
#: **Recorded WITHOUT parking, which no other code here is.** Every other
#: blocker is minted by `orchestrator._to_needs_user` or `_to_fault_stop`, and
#: both of those stop something: a park holds the session open for an answer, a
#: fault stop ends the run. Neither is right here — the loop is working, on
#: other tasks, and the failure being reported is one task's ABSENCE from the
#: queue. So the record is filed on its own and the loop carries on. Two
#: consequences worth knowing before adding a second code like this:
#:
#:   * it is invisible to `test_m1_hardening._emitted_blocker_codes`, which
#:     AST-walks those two emitters only, so it is deliberately absent from
#:     `cli._RESOLUTION_PRECONDITIONS` (that dict's keys must all be emitted
#:     codes) and an operator can answer it with no precondition recheck;
#:   * `answer` will report `task_not_blocked` while resolving it, because the
#:     task is `in_progress` rather than `blocked` — this code never changes a
#:     task's status. The question text says so and names `release` as the
#:     command that moves the task.
STRANDED_AFTER_FAULT = "stranded_after_environment_fault"


# ---- autonomous recovery (halt-02, 2026-08-25) ------------------------------
#
# Measured 2026-08-24 over 131 resolved blocker records: 411.8h of the loop's
# life was spent parked, and 30.6% of the window where every round is timed was
# the loop waiting for an operator. A transport or environment fault is the
# worst of those waits, because there is nothing to DECIDE — the loop already
# knows the remedy, it simply refuses to move without a human saying so.
#
# Autonomous mode (`config.AutonomyConfig`, default OFF) changes that for the
# codes named below and for NOTHING else. Two stages, in order:
#
#   1. RETRY the recovery path that already exists, bounded, durably counted;
#   2. when that path is exhausted, SET THE TASK ASIDE — park `task_fatal`
#      naming the task in flight, which is the existing quarantine
#      `cli._handle_parked_task` already knows how to work past — so the loop
#      keeps going on the rest of the roadmap instead of stopping.
#
# Stage 2 is the point. Stage 1 is only worth doing where a recovery path
# genuinely exists, which is why `max_attempts` is 0 for three of the SIX
# entries and the reason is written into each entry rather than left implied.
#
# Six entries, not seven: halt-02 named seven codes, and one of them — the
# conversation-rotation failure brw-15 retired — has no producer left in
# `orchestrator.py`. A code no live provider can raise is REMOVED rather than
# automated, so it is absent here by decision, and
# `test_autonomous_recovery.py` asserts both that absence and that every code
# that IS here is one the orchestrator can actually emit.


#: The parks autonomous mode must NEVER touch, whatever else is configured.
#:
#: Each one means an agent wrote outside its worker repository, or the checkout
#: is not what the loop believes it to be. Continuing past any of them corrupts
#: the tree every later task builds on, so "retry and step aside" is exactly the
#: wrong answer: stepping aside from a compromised checkout leaves the
#: compromise in place and starts the next task on top of it.
#:
#: An ALLOWLIST is what actually enforces this (`AUTONOMOUS_RECOVERIES` below);
#: this frozenset is the second, redundant lock — `autonomous_recovery` refuses
#: a member outright, and a test asserts the two sets are disjoint, so adding a
#: hard halt to the table is a failing test rather than a silent automation.
HARD_HALT_CODES = frozenset({
    "checkout_escape_detected",
    "worker_isolation_violation",
    "primary_checkout_dirty",
    "approved_path_symlink_traversal",
    "prompt_integrity_mismatch",
})

#: The SESSION ceiling — a budget that belongs to the run, not to a task, and
#: therefore the one exhaustion autonomous mode must NOT set a task aside for
#: (halt-01, 2026-08-26).
#:
#: `iteration_budget_exhausted` is raised by `orchestrator._step_ready` from
#: `policy.check_iteration_budget(state.iteration + 1)`. Two facts make it
#: different in kind from every entry in `EXHAUSTED_BUDGET_RECOVERIES`:
#:
#:   * there is no task at fault. The count is the RUN's, so quarantining
#:     whichever task happened to be in flight blames it for a limit it did not
#:     spend; and
#:   * skipping one would not help — it would do the opposite. Setting a task
#:     aside deletes the session file (`cli._handle_parked_task`), so the next
#:     iteration starts a fresh `LoopState` with `iteration = 0` and a full
#:     budget. Automating this code would make `policy.max_iterations`
#:     structurally unenforceable AND walk the backlog blocking every task on
#:     the way. That is the exact reading auto-02's own text corrected, and the
#:     reason this frozenset exists rather than the code merely being left out
#:     of the table: an omission is indistinguishable from an oversight, and the
#:     next person to grow the table would add it.
#:
#: The contrast is what makes the other seven safe: each of those rides either a
#: per-task record (`attempt_count_ceiling`, `review_round_cap`) or a
#: CONSECUTIVE counter the same session reset clears (`parse_retries`,
#: `policy_denials`, `consecutive_failures`), so continuing genuinely continues.
#:
#: Enforced the same two ways `HARD_HALT_CODES` is: `autonomous_recovery`
#: refuses a member before consulting the table, and a test asserts the two sets
#: are disjoint — so adding this code to the table is a failing test rather than
#: a silent automation.
SESSION_CEILING_CODES = frozenset({
    "iteration_budget_exhausted",
})

#: Re-enter the phase the park itself declares resumable (`resume_phase`) —
#: precisely what `run --retry` does, and the loop's own statement that this
#: session can continue by stepping that phase again.
RECOVER_BY_RESUMING = "resume_phase"

#: Re-issue the SAME request id (`run --resubmit`): clear the request's
#: `send_attempted` mark and re-enter `submitting`. The one action that
#: knowingly risks a duplicate, and it is authorized for exactly one code.
RECOVER_BY_RESUBMITTING = "resubmit"

#: No in-process recovery exists, so the recovery path is exhausted the moment
#: the fault is raised and stage 2 fires immediately. NOT a synonym for "do
#: nothing": setting the task aside and continuing is the whole remedy for
#: these, and it is the half that was missing.
RECOVER_UNAVAILABLE = "none"

#: ARCHIVE the stale record this park is holding, rebuild at the CURRENT head,
#: and re-dispatch (halt-03, 2026-08-25). The one action every entry in
#: `STALE_RECORD_RECOVERIES` takes, and the reason that dict needs no second
#: verb: the six codes differ only in WHICH record is stale, which is what
#: `AutonomousRecovery.stale_record` names.
#:
#: Distinct from `RECOVER_BY_RESUMING` in the thing that makes it safe to
#: automate. A resume is a HOPE that a transient fault has passed, so it is
#: bounded by a retry budget and nothing else; a rebuild REMOVES the record
#: that caused the fault, so the identical fault cannot recur off the same
#: record. Every rebuild archives rather than deletes — the execution record
#: goes to `executions/archive/` and its worker to `quarantine/` through
#: `worktask.retire_execution`, and a session pointer is written into the
#: transcript before it is cleared — so "no operator step" never means "no
#: record of what was discarded".
RECOVER_BY_REBUILDING_AT_HEAD = "rebuild_at_head"

#: RETURN THE REFUSAL TO THE AGENT AS FEEDBACK (halt-04, 2026-09-01): re-dispatch
#: the task as a `revise` whose `feedback` is the refusal text this park was
#: about to show a human. The one action every entry in
#: `REFUSED_WORK_RECOVERIES` takes.
#:
#: Distinct from `RECOVER_BY_RESUMING` in what it re-enters. A resume steps the
#: SAME phase again and hopes a transient fault has passed; this runs a NEW round
#: of work, with the reason the last one was refused in front of the agent that
#: has to fix it. Nothing about the refused candidate is rolled back or
#: discarded: `revise` continues the same execution record, the same worker
#: repository and the same task branch, so the refused commit stays exactly where
#: it is and the next round is built on top of it — the "park and report, never
#: undo" rule every one of these sites already states.
#:
#: Distinct from `RECOVER_BY_REBUILDING_AT_HEAD` in what it costs and therefore
#: in how it is bounded. A rebuild REMOVES the record that caused the fault, so
#: the identical fault cannot recur off it; a revise removes nothing, so a second
#: identical refusal is genuinely possible and the whole design turns on refusing
#: it. See the repeat guard in the module docstring, and `max_attempts = 1` on
#: every entry below.
RECOVER_BY_REVISING = "revise"


# ---- WHICH record a rebuild archives ----------------------------------------
#
# One value per shape, never a catch-all, because the shapes are not
# interchangeable and treating them as one is how a rebuild destroys work.
# `task_base_behind_head`'s record holds a reviewed candidate and has to go
# through recut-01's five refusals before anything moves;
# `push_candidate_stale`'s record is an APPROVAL BINDING and archiving the
# execution beneath it would discard a live candidate over a stale pointer.
# `orchestrator._autonomous_rebuild` dispatches on these and REFUSES an
# unrecognised one, so a future entry that forgets to name its record parks
# exactly as the loop parks today rather than falling into someone else's
# handler.

#: `TaskExecution` on disk: its `task_base_sha` is behind the branch head and
#: the reviewed candidate could not be carried past it. Archived through
#: `release_task_to_pending(move=registry.recut)` — recut-01's path, with
#: recut-01's refusals and recut-01's durable `MAX_TASK_RECUTS` bound.
STALE_EXECUTION_RECORD = "execution_record"

#: A postcommit / changeset APPROVAL BINDING naming a candidate that is no
#: longer this task's current one, or that no longer resolves at all. The
#: binding is dropped and the REVIEW IT WAS STANDING IN FRONT OF IS REBUILT:
#: `orchestrator._rebuild_task_review_at_head` re-presents the candidate the
#: execution record actually holds as a fresh, verified-bindable
#: `postcommit_review` packet, so the very next round can be approved and
#: published. Dropping the binding alone — which the first cut of halt-03 did —
#: leaves the next request carrying none of the identifiers an approval binds
#: by, and a candidate nothing can publish is a park performed rather than
#: avoided.
#:
#: The execution record underneath is still not archived in the ordinary case,
#: for the reason the `push_candidate_stale` entry gives. The ONE exception is
#: the shape where nothing can be rebuilt at all AND git said so — the worker
#: repository the record names answers that its object database does not hold
#: the record's OWN candidate — and there the rebuild routes to
#: `STALE_EXECUTION_RECORD`'s path, recut-01's refusals and cap included, rather
#: than emitting an unbound request. Every weaker reading of "does not resolve"
#: parks: a probe that could not answer, a record naming no candidate, and a
#: record naming no worker repository to ask in are each an unanswered question,
#: not an absent object.
STALE_PUSH_BINDING = "push_binding"

#: The PACKET standing between an operator-queued changeset review
#: (`LoopState.changeset`) and the reviewer: `LoopState.outbox` does not carry
#: the four identifiers an approval binds by, so no round spent on it could ever
#: publish. The queue entry is KEPT and the packet is rebuilt around it
#: (`orchestrator._rebuild_changeset_packet_at_head`) — dropping the entry
#: instead would send the unbindable payload as an ordinary unbound request and
#: leave the operator's candidate unpublishable for the rest of the session,
#: which is discarding the review rather than rebuilding it.
STALE_QUEUED_REVIEW = "queued_review"

#: `LoopState.current_task` pointing at an audit pseudo-task with no audit unit
#: on record, so the `revise` answering it names nothing. Dropped; the next
#: `audit` mints a fresh unit at the current head.
STALE_AUDIT_POINTER = "audit_pointer"

#: The loop's own half-finished ROUND (`last_response` / `pending_request`)
#: after a `StateError` proved the session's bookkeeping inconsistent. Dropped
#: so the next `ready` step rebuilds the round from the current head. The only
#: rebuild that names no durable record, and therefore the only one whose
#: blocker is NOT closed by the rebuild itself — see `orchestrator.
#: _discard_inconsistent_round`.
STALE_SESSION_ROUND = "session_round"


@dataclass(frozen=True)
class AutonomousRecovery:
    """What autonomous mode does about one blocker code, and why.

    `max_attempts` counts RETRIES, not occurrences: at 2, the first and second
    occurrence of the condition each re-enter the recovery path and the third
    sets the task aside. At 0 the first occurrence sets it aside. A REBUILD is
    counted the same way, and at 1: rebuilding the same record twice inside one
    open episode means the first rebuild did not take, and a second identical
    attempt buys no new evidence.

    `stale_record` is required for `RECOVER_BY_REBUILDING_AT_HEAD` and empty for
    every other action. Empty is the fail-closed value: `orchestrator.
    _autonomous_rebuild` refuses a record kind it does not recognise, so an
    entry that names none rebuilds nothing and the loop parks as it does today.
    """

    code: str
    action: str
    max_attempts: int
    why: str
    stale_record: str = ""


#: halt-02's half: a TRANSPORT or ENVIRONMENT fault, answered by re-entering the
#: recovery path that already exists and then stepping the task aside.
TRANSPORT_RECOVERIES: dict[str, AutonomousRecovery] = {
    entry.code: entry
    for entry in (
        AutonomousRecovery(
            code="login_expired",
            action=RECOVER_BY_RESUMING,
            max_attempts=2,
            why=(
                "The park already carries the phase it was raised in as "
                "`resume_phase`, and `run()` drops the client before parking — "
                "so re-entering that phase builds a fresh client and re-reads "
                "the session, which is the entire manual remedy minus the wait "
                "for a human. Nothing is sent by the re-entry that was not "
                "about to be sent anyway."
            ),
        ),
        AutonomousRecovery(
            code="git_unavailable_in_ready",
            action=RECOVER_BY_RESUMING,
            max_attempts=2,
            why=(
                "Raised while BUILDING a request, before one exists: the outbox "
                "is intact and nothing has been sent, which is why the park is "
                "retryable in the first place. Re-entering `ready` re-runs the "
                "context build against a git that may simply have been busy "
                "(an index lock, a concurrent fetch)."
            ),
        ),
        AutonomousRecovery(
            code="submission_ambiguous",
            action=RECOVER_BY_RESUBMITTING,
            max_attempts=1,
            why=(
                "The one code whose automation TRADES a risk rather than "
                "removing one: acceptance is unknown, so re-issuing may post a "
                "duplicate. Autonomous mode takes that trade deliberately — a "
                "possible duplicate request beats a stopped loop — and takes it "
                "ONCE, because the second identical resend buys no new evidence "
                "and doubles the duplicate. Still reachable: "
                "`codex.app_server_conversation` declines `idempotent_submit` "
                "and reconciles an appended-but-unanswered turn to False."
            ),
        ),
        AutonomousRecovery(
            code="worker_environment_drift",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "The park declares no `resume_phase` and the round's own "
                "`last_response` is cleared before it, so there is no phase to "
                "step again. The existing remedy is an operator repairing the "
                "shared git environment (a hook, `core.hooksPath`, a url "
                "rewrite) — the loop must not undo any of those on its own. So "
                "the recovery path is empty and the task is set aside at once. "
                "NOTE the classification this overrides: the park site argues "
                "loop_fatal because the drift affects every task, and under "
                "autonomous mode each task is instead set aside in turn."
            ),
        ),
        AutonomousRecovery(
            code="publisher_url_drift",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "SECURITY-SHAPED, and the reason `max_attempts` is 0 rather "
                "than small. The only recovery is `reprovision-publisher "
                "--confirm`, an operator's confirmation that a NEW push "
                "destination is correct; a loop that could take it would be a "
                "loop that can redirect its own publication. Re-running the "
                "snapshot-vs-live comparison in the same process is not a "
                "recovery either, it is the identical read seconds later. "
                "Nothing is published, ever, on this path."
            ),
        ),
        AutonomousRecovery(
            code="crash_reconciliation_ambiguous",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "`reconcile_after_crash` is deterministic over an intent the "
                "park deliberately does NOT clear, so a re-run returns "
                "AMBIGUOUS again by construction — a retry here is theatre that "
                "costs a reviewer round. Already `task_fatal` at its park site, "
                "so autonomous mode changes nothing about it except to state "
                "that plainly and to hold it to the same table as the rest."
            ),
        ),
    )
}


# ---- halt-03's half: a STALE RECORD, rebuilt at the current head -------------
#
# MEASURED 2026-08-24 over 131 resolved blocker records: `task_base_behind_head`
# is the second largest cause of parked time — 15 parks, 18.5h, median 0.54h.
# The median is the tell. A park that is cleared as fast as it is seen is not a
# decision anybody makes; the operator reads the blocker, archives the execution
# record the question NAMES, and the loop cuts the task again at the current
# head. Every code below has that shape: the loop already knows which record is
# stale, already names the remedy in its own park text, and simply refuses to
# perform it.
#
# All six are `max_attempts=1`. The budget is not the interesting bound here —
# a rebuild removes the record that caused the fault, so the identical fault
# cannot recur off the same record — and where a durable bound IS needed it is
# the one that already exists: `STALE_EXECUTION_RECORD` goes through
# `registry.recut`, so it is capped per task by `orchestrator.MAX_TASK_RECUTS`
# across episodes, not merely within one.
STALE_RECORD_RECOVERIES: dict[str, AutonomousRecovery] = {
    entry.code: entry
    for entry in (
        AutonomousRecovery(
            code="task_base_behind_head",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_EXECUTION_RECORD,
            why=(
                "The park's OWN text already names the remedy — 'archive "
                ".autoloop/executions/<task>.json to start fresh at the current "
                "head' — and recut-01 already implements exactly that, "
                "refusals included. So this rebuilds through "
                "`release_task_to_pending(move=registry.recut)` rather than "
                "archiving anything itself: a published candidate is refused, a "
                "candidate with a verdict still in flight is refused, an "
                "unreadable record is refused, and the cut is charged to "
                "`recut_count` so `MAX_TASK_RECUTS` caps it per task. A second "
                "archival mechanism would have had to re-earn all four."
            ),
        ),
        AutonomousRecovery(
            code="push_candidate_stale",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_PUSH_BINDING,
            why=(
                "The park says it in its own words: 'a later round advanced it, "
                "or the execution record is gone'. The stale thing is the "
                "APPROVAL BINDING, and the execution record underneath it "
                "usually holds a live candidate a reviewer is about to approve — "
                "so this drops the binding and the ledger entries that could "
                "re-resolve it, and archives nothing. The remedy the park asks "
                "for, 're-review the current state before approving again', is "
                "then PERFORMED rather than requested: the record's current "
                "candidate is re-rendered as a `postcommit_review` packet, "
                "verified to carry the four identifiers `_current_pending_"
                "postcommit` binds on, and dispatched — so the next round is a "
                "real review whose approval publishes. Dropping the binding and "
                "queueing a sentence, as the first cut did, left that candidate "
                "unpublishable for the session."
            ),
        ),
        AutonomousRecovery(
            code="push_candidate_unresolvable",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_PUSH_BINDING,
            why=(
                "The same stale record as `push_candidate_stale`, reached by the "
                "other road: the approved sha does not resolve at all. TWO park "
                "sites emit it — the task push, which names a task, and the "
                "changeset push, which names none — so the rebuild has to handle "
                "a binding with no task behind it, and does. On the TASK arm the "
                "record's own candidate is usually that same unresolvable "
                "commit, so there is nothing to re-present and the rebuild routes "
                "to the archive-and-recut path rather than emitting an unbound "
                "request. On the CHANGESET arm the queue entry decides, and only "
                "on GIT'S OWN answer about it: a candidate the repository "
                "reports it holds means only the packet was stale, so the packet "
                "is rebuilt around it; one the repository reports it does not "
                "hold is dropped, after its whole record is written to the "
                "transcript. A question that went unanswered — no gateway, a "
                "probe that died, a repository that is not there — is neither, "
                "and parks with the operator's review untouched, because that "
                "record exists nowhere else afterwards. Nothing is pushed on "
                "this path, ever."
            ),
        ),
        AutonomousRecovery(
            code="state_inconsistent",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_SESSION_ROUND,
            why=(
                "The one entry whose stale record is the loop's OWN round rather "
                "than anything on disk, and the one that has to be bounded by a "
                "budget rather than by removal — the underlying inconsistency "
                "can outlive the round that tripped over it, so its blocker is "
                "deliberately left OPEN by the rebuild and closed only by a step "
                "that afterwards COMPLETES. A second occurrence with no "
                "completed step in between therefore finds the allowance spent "
                "and parks. It also refuses to fire at all for the corrupt "
                "subclass: `StateCorruptError` reaches this same handler, and "
                "rebuilding a round on top of a store that cannot be READ is the "
                "fail-open this whole design is built to avoid, so the park site "
                "passes `recoverable=False` for it."
            ),
        ),
        AutonomousRecovery(
            code="audit_revise_no_record",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_AUDIT_POINTER,
            why=(
                "A `revise` of the audit pseudo-task while `state.current_task` "
                "holds no audit unit id: the directive names a record that was "
                "never minted, and the park's own remedy is 'Run `audit` first'. "
                "An audit unit is synthetic and per-iteration, so there is "
                "nothing to salvage and nothing to return to a queue — dropping "
                "the pointer and telling the reviewer to run `audit` IS the "
                "rebuild, and the fresh unit is cut at the current head by "
                "construction."
            ),
        ),
        AutonomousRecovery(
            code="changeset_binding_missing",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_QUEUED_REVIEW,
            why=(
                "A queued changeset review whose PACKET cannot carry the four "
                "identifiers an approval binds by. It is raised BEFORE anything "
                "is sent and refuses every round for as long as the payload "
                "stands, so this is the one code here that halts the loop "
                "indefinitely rather than costing it a round. The stale record "
                "is the packet, never the queue entry: `build_changeset_packet` "
                "always stamps the four identifiers and `review-changeset` sets "
                "no `outbox_diff`, so the queued packet always binds and this "
                "fault can only be raised once something ELSE has taken the "
                "outbox (a corrective re-prompt, a plan request, a later task "
                "review packet). So the entry is kept and the packet is rebuilt "
                "around it, which is the park's own remedy — 're-queue with "
                "`review-changeset`' — performed rather than requested. Dropping "
                "the entry, as the first cut of this did, sent the unbindable "
                "payload unbound and left the operator's candidate "
                "unpublishable: a review discarded, not rebuilt."
            ),
        ),
    )
}


# ---- halt-01's half: an EXHAUSTED BUDGET or CEILING, set aside --------------
#
# A budget or a ceiling means STOP CHURNING ON THIS TASK, not stop everything.
# Six of the seven codes below stop everything today: four park `loop_fatal`
# and one ends the run through `_to_fault_stop`, so one task's spent allowance
# takes the whole roadmap down with it.
#
# Every entry is `RECOVER_UNAVAILABLE` with `max_attempts = 0`, and that is a
# statement rather than a gap. These are not transport faults that might pass
# and not stale records that can be rebuilt — the counter really did reach its
# limit, and the only thing that could change the answer is an operator raising
# the limit or rewriting the task. So there is nothing to retry, the recovery
# path is exhausted the moment the fault is raised, and the set-aside — the half
# that was missing — fires at once.
#
# TWO of the seven already classify `task_fatal` at their own park sites
# (`attempt_count_ceiling`, `review_round_cap`), so their entries change no
# behaviour in either flag position. They are here anyway, and deliberately: the
# claim halt-01 makes is about all seven, an entry is what holds a code to this
# table and to `orchestrator._autonomous_set_aside_task`'s active-task
# validation, and a guarantee that happens to hold because two unrelated park
# sites chose well is not a guarantee anybody is keeping.
#
# WHAT THE OTHERS BUY, stated precisely, because "the loop keeps going" is only
# true if the next round starts from a clean count. Setting a task aside deletes
# the session file (`cli._handle_parked_task`), so the next iteration builds a
# fresh `LoopState`: `parse_retries`, `policy_denials` and
# `consecutive_failures` all start at zero, and the next task is not charged for
# the previous one's churn. That same session reset is exactly why
# `iteration_budget_exhausted` is NOT here — see `SESSION_CEILING_CODES`.
#
# AND WHAT IT COSTS, stated rather than left to be discovered. A condition that
# is genuinely loop-wide rather than task-shaped — a git environment that is
# broken for every worker, a reviewer that refuses everything — walks the
# backlog setting each task aside in turn, until `run --continuous` finds
# nothing ready and exits 0 printing the open blockers (the exhaustion path in
# `docs/AUTOLOOP.md` §9c). That is bounded and visible, but it is more churn
# than the single stop it replaces, and it is the trade the flag turns on. It is
# the same trade `worker_environment_drift` already carries.
EXHAUSTED_BUDGET_RECOVERIES: dict[str, AutonomousRecovery] = {
    entry.code: entry
    for entry in (
        AutonomousRecovery(
            code="attempt_count_ceiling",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "MEASURED 2026-08-24 over 131 resolved blocker records: the "
                "single largest cause of operator-blocked time (12 parks, "
                "32.0h). ceil-01 already asks the REVIEWER to classify a task "
                "at its ceiling, and this park is what is left when that ask is "
                "unavailable or every remedy is spent — so there is nothing "
                "further to retry and the two compose exactly as halt-01 states "
                "them: ask first, set aside if the answer does not resolve it. "
                "Its site already classifies `task_fatal`, so this entry changes "
                "no behaviour; what it changes is that the guarantee is on "
                "record and tested instead of incidental."
            ),
        ),
        AutonomousRecovery(
            code="review_round_cap",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "`policy.max_review_rounds` is counted on the TASK's execution "
                "record, so the exhaustion is task-shaped by construction and no "
                "other task is implicated. A further round is refused before it "
                "is sent, which is the whole point of the cap, so there is no "
                "recovery path to re-enter. Like `attempt_count_ceiling` its "
                "site is already `task_fatal`; the entry is what holds it to "
                "this table."
            ),
        ),
        AutonomousRecovery(
            code="parse_budget_exhausted",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "`state.parse_retries` counts CONSECUTIVE malformed replies, and "
                "the park's own text names the remedy as `run --answer` — an "
                "operator writing a message to the reviewer, which the loop must "
                "not compose for itself. Re-parsing the same stored response "
                "would fail identically, so the recovery path is empty rather "
                "than merely unattractive. The set-aside is what helps: the "
                "session is discarded with the task, so the counter starts at "
                "zero for the next one."
            ),
        ),
        AutonomousRecovery(
            code="plan_denial_budget_exhausted",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "Repeated task-GRAPH rejections of a `plan` reply (a cycle, a "
                "dependency on an id that does not exist, a duplicate). Nothing "
                "in-process can produce a different plan, so no retry. NOTE the "
                "shape this one is often raised in: a `plan` round frequently "
                "has no task in flight at all, and there the set-aside resolves "
                "nothing and the loop parks exactly as it does today — "
                "`_autonomous_set_aside_task` returning `None` is a real answer, "
                "not a failure."
            ),
        ),
        AutonomousRecovery(
            code="policy_denial_budget_exhausted",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "THE ONE MEMBER REACHED THROUGH `_to_fault_stop` RATHER THAN A "
                "PARK, and the reason that helper had to learn about this table "
                "at all. Its site argues `loop_fatal` because a reviewer that "
                "spent the denial budget is not a per-task condition — but the "
                "denials are overwhelmingly ABOUT one task (a missing "
                "decomposition, a quarantined id, a ceiling classification that "
                "was not given), so setting that task aside removes the thing "
                "the reviewer kept being refused about. Under autonomous mode "
                "the fault stop becomes the set-aside park, and ONLY when a "
                "set-aside is actually granted: a refused one keeps the fault "
                "stop, never a `loop_fatal` park holding the session open for an "
                "answer nobody can give."
            ),
        ),
        AutonomousRecovery(
            code="review_mismatch_budget_exhausted",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "Approvals that keep arriving stamped for a tree that has since "
                "moved. Its site deliberately PARKS rather than stopping, "
                "because the repository moving under the loop has a human-side "
                "explanation — and that reasoning is untouched here: the "
                "question is still recorded, still listed by `blockers`, still "
                "answerable. What changes is that one task is quarantined "
                "instead of the whole roadmap. No retry, because the correction "
                "round it would re-enter is the one that already ran out."
            ),
        ),
        AutonomousRecovery(
            code="git_failure_budget_exhausted",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "`state.consecutive_failures` past `max_consecutive_failures` "
                "outside `ready`/`delivering` (those two have their own "
                "retryable parks, `git_unavailable_in_*`). The budget exists "
                "because retrying git already failed that many times, so a "
                "further in-process retry is the thing the budget just refused. "
                "Its `cli._RESOLUTION_PRECONDITIONS` entry is unchanged, so an "
                "operator answering the record still gets the browser recheck "
                "they always did."
            ),
        ),
    )
}


# ---- halt-04's half: REFUSED WORK, returned to the agent as feedback --------
#
# A refusal is feedback. Every code below is the loop telling an agent that what
# it produced cannot be accepted and WHY — and then parking, so that a human can
# read the reason and type it back in. That relay is the whole of the operator
# step, which is why these are worth automating and why the automation is a
# `revise` rather than a retry: nothing here will pass on a second reading of the
# same tree.
#
# MEASURED 2026-08-24 over 131 resolved blocker records:
# `post_commit_verification_failed` 7 parks / 14.0h; `review_feedback_unchanged`
# 4 parks / 35.4h and the highest MEDIAN of any code at 5.92h — an operator who
# does not know what to do with it quickly.
#
# EVERY ENTRY IS `max_attempts = 1`, and that number is the claim. One revise per
# REFUSAL — per `refusal_identity`, not per (task, code) — metered on
# `BlockerStore.refusal_revises`, which counts every record on disk and is blind
# to phase, so the same complaint raised one phase along keeps spending the same
# allowance rather than buying a second. `review_feedback_unchanged` exists as a
# code precisely because a reviewer repeating itself is a recognised failure;
# resending without that bound would convert a park into an infinite loop, which
# is strictly worse than the park.
#
# KEYED ON THE REFUSAL BECAUSE A CODE IS NOT A COMPLAINT.
# `post_commit_verification_failed` names five different checks and
# `commit_refused` covers every reason git declined a commit; a second occurrence
# saying something genuinely different is feedback the agent has never been
# given, and refusing to hand it over because a sibling fault already spent the
# code's allowance would park work that had an answer. What that costs is that a
# code whose text changes every round is not bounded HERE, which is why the three
# bounds below are load-bearing rather than decorative.
#
# THREE FURTHER BOUNDS ALREADY EXIST and are not re-implemented here, because a
# fourth copy of a limit is a fourth place for it to disagree with itself:
#
#   * `orchestrator._revise_feedback_is_unchanged` refuses a `revise` whose
#     feedback repeats the last one verbatim — which is exactly what a self-issued
#     revise carrying an unchanged refusal would be;
#   * `policy.max_review_rounds` refuses the round outright once the cap is
#     reached (`review_round_cap`, itself a set-aside code above);
#   * `MAX_TASK_ATTEMPTS` is charged before the executor runs, so every self-issued
#     revise costs an attempt exactly as a reviewer's does and ends at
#     `attempt_count_ceiling` — also a set-aside code above.
#
# WHAT THIS DOES NOT REACH, stated positively rather than left as a gap. Two of
# the seven are raised at TWO sites: `push_not_descendant` and
# `push_tree_mismatch` each have a changeset arm (`_dispatch_changeset_push`)
# that names no task at all, because an operator's changeset has no roadmap task
# by construction. A `revise` needs a task to revise, so that arm parks exactly
# as it does today — `_autonomous_set_aside_task` answers `None` and the plan is
# dropped, which is the same fail-closed path a park with no round in flight
# already takes.
REFUSED_WORK_RECOVERIES: dict[str, AutonomousRecovery] = {
    entry.code: entry
    for entry in (
        AutonomousRecovery(
            code="post_commit_verification_failed",
            action=RECOVER_BY_REVISING,
            max_attempts=1,
            why=(
                "MEASURED 2026-08-24: 7 parks, 14.0h. The park text already IS "
                "the feedback — it names every failing check by name (ancestry, "
                "an empty commit range, a worktree left dirty, failing "
                "post-commit validation, validation that mutated the tree) and "
                "the operator's whole step is relaying that list to the agent "
                "that wrote the commit. The candidate is neither rolled back nor "
                "pushed either way, so a revise round starts from exactly the "
                "tree the refusal describes."
            ),
        ),
        AutonomousRecovery(
            code="commit_refused",
            action=RECOVER_BY_REVISING,
            max_attempts=1,
            why=(
                "The commit was refused BEFORE it happened — HEAD drift, or a "
                "path list git would not accept — so nothing was committed and "
                "nothing was rolled back. The exception text is the whole "
                "diagnosis and the next round is the remedy. The one code here "
                "whose round produced no candidate at all, which changes nothing "
                "about the shape: `revise` continues the same execution record, "
                "and a record with no candidate simply cuts its first one."
            ),
        ),
        AutonomousRecovery(
            code="review_feedback_unchanged",
            action=RECOVER_BY_REVISING,
            max_attempts=1,
            why=(
                "MEASURED 2026-08-24: 4 parks, 35.4h, and the highest median of "
                "any code at 5.92h — the operator does not know what to do with "
                "it quickly. The park says the reviewer asked for the same change "
                "twice, so the executor did not alter what was asked; the "
                "feedback IS on record and IS quoted in the park text. Returning "
                "it once, labelled as a refusal rather than as a fresh reviewer "
                "request, is the one move that can change the outcome. THE "
                "SHARPEST CASE FOR THE BOUND: a second unchanged round is the "
                "definition of this code, so what arrives the second time is the "
                "identical refusal — which is exactly what the identity meter "
                "recognises, and what `_revise_feedback_is_unchanged` would catch "
                "even if the meter did not."
            ),
        ),
        AutonomousRecovery(
            code="review_packet_build_failed",
            action=RECOVER_BY_REVISING,
            max_attempts=1,
            why=(
                "The candidate passed post-commit review and could not be "
                "PRESENTED — most often a range diff over the render cap, which "
                "is a property of what was committed and therefore something the "
                "next round can change. It is raised at two sites and both are "
                "answerable by the agent: the build failure itself, and the "
                "stat-only split ask the reviewer declined. Neither rolls the "
                "commit back and neither pushes anything, so the revise is a "
                "further round on the same branch and nothing else."
            ),
        ),
        AutonomousRecovery(
            code="approved_paths_missing",
            action=RECOVER_BY_REVISING,
            max_attempts=1,
            why=(
                "THE ENTRY WHOSE REVISE IS EXPECTED TO BE RE-REFUSED, and it is "
                "here deliberately rather than in spite of that. The task carries "
                "no `approved_paths`, the gate that says so runs before the "
                "executor, and an agent cannot widen its own authorization — that "
                "circularity is M1 finding #2 and it stays closed. So the "
                "self-issued revise reaches the same gate, is refused identically, "
                "and the repeat guard sets the task aside: one dispatch that runs "
                "no agent and touches no repository, and then a quarantine the "
                "loop works past. Skipping it because the loop can predict the "
                "answer would be a pre-flight judgement about whether feedback "
                "will help, which is exactly the judgement this table is not "
                "entitled to make."
            ),
        ),
        AutonomousRecovery(
            code="push_not_descendant",
            action=RECOVER_BY_REVISING,
            max_attempts=1,
            why=(
                "An approved candidate that is not a descendant of the task base: "
                "the reviewed line and the recorded line have parted, so nothing "
                "is pushed. TASK ARM ONLY — the changeset arm names no task and "
                "parks as it does today (see the note above this table). A revise "
                "re-cuts the round on the recorded base and produces a candidate "
                "an approval can be bound to, which is what the park asks a human "
                "to arrange by hand."
            ),
        ),
        AutonomousRecovery(
            code="push_tree_mismatch",
            action=RECOVER_BY_REVISING,
            max_attempts=1,
            why=(
                "The reviewed candidate's TREE is no longer the tree that was "
                "reviewed, so the approval no longer describes what would be "
                "published and nothing is pushed. Same two arms and the same "
                "restriction as `push_not_descendant`: the task arm revises, the "
                "changeset arm parks. The remedy is a fresh round producing a "
                "candidate that can be reviewed again — never a push of the tree "
                "that drifted, which this path cannot reach at all."
            ),
        ),
    )
}


#: THE table `autonomous_recovery` reads — halt-02's transport half, halt-03's
#: stale-record half, halt-01's exhausted-budget half and halt-04's refused-work
#: half, merged. One lookup, one set of refusals: a caller that consulted
#: `STALE_RECORD_RECOVERIES`, `EXHAUSTED_BUDGET_RECOVERIES` or
#: `REFUSED_WORK_RECOVERIES` directly would bypass the refusals in
#: `autonomous_recovery`, which is why nothing does.
AUTONOMOUS_RECOVERIES: dict[str, AutonomousRecovery] = {
    **TRANSPORT_RECOVERIES,
    **STALE_RECORD_RECOVERIES,
    **EXHAUSTED_BUDGET_RECOVERIES,
    **REFUSED_WORK_RECOVERIES,
}


def refusal_identity(code: str, question: str, detail: str) -> str:
    """A stable digest of ONE refusal — the thing the repeat guard compares
    (halt-04).

    Over `code`, `question` and `detail` together, whitespace-collapsed and
    case-folded, because those three are the whole of what `_to_needs_user` was
    given and therefore the whole of what the agent is about to be shown. Same
    normalisation rule and same deliberate non-fuzziness as
    `orchestrator._normalise_feedback`: a re-wrap must not read as a new
    refusal, and two genuinely different complaints must always be allowed to
    be different.

    **This IS the repeat guard's key**, and its exactness is the point: the
    guard has to distinguish "the loop is saying the same thing again", which is
    a loop and must stop, from "the loop is saying something new", which is
    feedback the agent has never been given. A fuzzy digest would silently
    collapse the second case into the first and park work that had an answer.

    What it deliberately does NOT bound is a code whose text changes every
    round — `post_commit_verification_failed` names a commit sha, so two
    occurrences of the same underlying fault can digest differently. That case
    is bounded elsewhere and on purpose: every self-issued revise costs an
    attempt from `MAX_TASK_ATTEMPTS` and a round from `policy.max_review_rounds`,
    both of which end in a set-aside code. Widening this digest to cover it would
    trade a bounded churn for a guard that cannot tell two faults apart.

    Returns `""` for a refusal with no text at all, which `refusal_revises` and
    `orchestrator._refusal_revise_budget` both read as "no identity to meter" —
    the textless refusal is refused outright rather than revised, so `""` can
    never be stored and two absences can never read as a match.
    """
    normalised = " ".join(f"{code}\n{question}\n{detail}".split()).strip().lower()
    if not normalised or normalised == code.strip().lower():
        return ""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def autonomous_recovery(code: str) -> AutonomousRecovery | None:
    """What autonomous mode does about `code`, or `None` for "park exactly as
    the loop parks today".

    `None` is the answer for every code that is not in the table, for a hard
    halt, for the SESSION ceiling, and for an empty/unknown one — the
    fail-closed direction, and the reason this is a function rather than a bare
    dict lookup at the call site: neither refusal can be forgotten by a caller
    that only remembers the dict.
    """
    if code in HARD_HALT_CODES or code in SESSION_CEILING_CODES:
        return None
    return AUTONOMOUS_RECOVERIES.get(code)


@dataclass
class Blocker:
    id: str                 # stable: f"blk-{task_id}-{n:03d}"
    task_id: str
    kind: str                # "task_fatal" | "loop_fatal"
    code: str                # short machine slug, e.g. "attempt_count_ceiling"
    question: str            # the operator-facing text (what _to_needs_user was given)
    detail: str              # extra context (paths, shas, reasons)
    phase: str               # loop phase when it happened
    created_at: str
    resolved_at: str | None = None
    answer: str | None = None
    #: How many times this exact condition has re-parked. A restart or retry
    #: that hits the same wall must UPDATE this record, never mint a second
    #: one — otherwise a crash-retry loop silently fills the queue with
    #: duplicates of one problem and the operator cannot see how many
    #: distinct things are actually wrong.
    recurrences: int = 1
    last_seen_at: str = ""
    #: The loop session that parked this. Recorded so "does this blocker
    #: belong to a session that has since been retired?" is a machine question
    #: rather than a timestamp inference — the only way to answer it before
    #: this field existed was to match `created_at` against a transcript line,
    #: by hand. Empty on records written before this field.
    session_id: str = ""
    #: Set ONLY by `archive_stale`. Distinct from `answer` on purpose: `answer`
    #: means "an operator responded to this question", and writing text there
    #: to clear a dead blocker would forge exactly the operator confirmation
    #: `_RESOLUTION_PRECONDITIONS` exists to require.
    archived_reason: str = ""
    #: Every `refusal_identity` this record has already been answered with a
    #: self-issued `revise` (halt-04) — the repeat guard's memory, and the only
    #: thing `refusal_revises` counts. Appended by `note_refusal_revise` at the
    #: moment the loop commits to a revise, never on a park that merely happened,
    #: so it records ACTIONS THE LOOP TOOK rather than occurrences it saw.
    #:
    #: Persisted on the blocker rather than on the execution record because a
    #: refusal is not always about a task that HAS one (`approved_paths_missing`
    #: is raised before any record exists) and because the blocker is the record
    #: that already survives a set-aside deleting the session file. Empty on
    #: every record written before this field existed and on every code
    #: autonomous mode does not answer with a revise, which `load` supplies by
    #: default and `refusal_revises` reads as "nothing spent here".
    revised_refusals: list[str] = field(default_factory=list)


#: Severity rank for primary selection — LOWER is more urgent. `loop_fatal`
#: means the loop cannot continue at all; `task_fatal` means one task is
#: parked and the loop works past it, so no number of task_fatal records is
#: as urgent as a single loop_fatal one, whatever their timestamps.
#:
#: An UNRECOGNISED kind ranks with `loop_fatal`, not after `task_fatal` —
#: same fail-closed direction as `orchestrator._to_needs_user`'s
#: `kind="loop_fatal"` default (`docs/AUTOLOOP.md` §9c): a record whose
#: severity we cannot read is treated as the loop-stopping one rather than
#: quietly sorted below every classified record, where it would be the last
#: thing an operator saw named as primary.
_KIND_RANK = {"loop_fatal": 0, "task_fatal": 1}
_UNCLASSIFIED_KIND_RANK = _KIND_RANK["loop_fatal"]


def _created_epoch(blocker: Blocker) -> float:
    """`created_at` as a comparable instant. An unparseable or empty stamp
    reads as `-inf`, i.e. the OLDEST possible — so a record we cannot date
    never wins the "most recent" tiebreak on the strength of being unreadable."""
    try:
        parsed = datetime.fromisoformat(blocker.created_at or "")
    except (TypeError, ValueError):
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def primary_sort_key(blocker: Blocker) -> tuple[int, float, str]:
    """THE ordering for "which open blocker is *the* one" — severity first,
    then most recent, then blocker id.

    One implementation on purpose. `health`, `heartbeat`, the CLI's status
    and exhaustion reports and any future autonomous routing all read the
    same open set for different reasons, and if each sorted it its own way
    the loop could describe one situation while acting on another. Ordering
    by whatever `Path.glob` returned is not a decision at all: `(` sorts
    before `b`, so `blk-(loop)-038.json` happening to be listed first is a
    coin flip that has nothing to do with which problem is worse. Observed
    2026-08-21, when a loop_fatal `parse_budget_exhausted` and a task_fatal
    `task_base_behind_head` were open together and a recovery script picked
    the second because the glob did.

    The three keys, in order:

    1. `_KIND_RANK` — loop_fatal (and anything unclassified) before task_fatal.
    2. Most recent `created_at` first: within one severity, the newest record
       describes the state the loop actually reached.
    3. Blocker id ascending, so two records written in the same second still
       order stably. Ids are monotonic per task (`blk-<task>-<NNN>`, zero
       padded), and ascending matches `all_blockers`' documented "oldest id
       first" rather than inventing a second convention.

    This chooses WHICH blocker is primary and nothing else. What action a
    given `code` deserves is a separate question, deliberately not answered
    here."""
    return (
        _KIND_RANK.get(blocker.kind, _UNCLASSIFIED_KIND_RANK),
        -_created_epoch(blocker),
        blocker.id,
    )


def by_severity(blockers: list[Blocker]) -> list[Blocker]:
    """`blockers` in `primary_sort_key` order — most urgent first, nothing
    dropped. For callers that already hold a list (e.g. a printer handed the
    open set); everything else should go through `BlockerStore`."""
    return sorted(blockers, key=primary_sort_key)


class BlockerStore:
    """One JSON file per blocker under `directory`. Atomic temp-file +
    `os.replace` writes — same pattern as `tasks.TaskStore`.

    Filenames double as ids (`<id>.json`), so `next_id` can derive the next
    free sequence number for a task purely by listing the directory — there
    is no separate counter file that could drift out of sync with what is
    actually on disk.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, blocker_id: str) -> Path:
        return self.directory / f"{blocker_id}.json"

    def save(self, blocker: Blocker) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(blocker.id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(blocker), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def load(self, blocker_id: str) -> Blocker | None:
        path = self._path(blocker_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            blocker = Blocker(**data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(f"blocker record {path} is unreadable: {exc}") from exc
        # halt-04: the meter's own field is type-checked once, here, rather than
        # at every read. `refusal_revises` calls `.count` on it, and a
        # hand-edited `revised_refusals` that is a string would count SUBSTRINGS
        # while one that is `null` would raise an AttributeError out of a park
        # handler. Either way the honest answer is the one this module gives
        # everywhere else: a record we cannot read is not a record that says
        # nothing was spent.
        if not isinstance(blocker.revised_refusals, list) or not all(
            isinstance(entry, str) for entry in blocker.revised_refusals
        ):
            raise StateCorruptError(
                f"blocker record {path} is unreadable: `revised_refusals` is not "
                "a list of refusal identities"
            )
        return blocker

    def next_id(self, task_id: str) -> str:
        """`f"blk-{task_id}-{n:03d}"` for the next free `n` — scans existing
        files for `task_id` rather than keeping a separate counter."""
        prefix = f"blk-{task_id}-"
        n = 0
        if self.directory.exists():
            for path in self.directory.glob(f"{prefix}*.json"):
                suffix = path.stem[len(prefix):]
                if suffix.isdigit():
                    n = max(n, int(suffix))
        return f"{prefix}{n + 1:03d}"

    def find_open(self, task_id: str, code: str, phase: str) -> "Blocker | None":
        """An OPEN blocker for the same (task, code, phase), or None.

        Identity is the CONDITION, not the occurrence: the same task hitting
        the same failure in the same phase is one blocker seen repeatedly,
        which is what the operator needs to answer once. A resolved blocker
        is deliberately not matched — if the condition returns after being
        answered, that is genuinely new and deserves its own record."""
        for existing in self.open_blockers():
            if (existing.task_id, existing.code, existing.phase) == (task_id, code, phase):
                return existing
        return None

    def record(self, *, task_id, kind, code, question, detail, phase, now,
               session_id: str = "") -> "Blocker":
        """Idempotent upsert. Returns the existing open blocker for this
        condition with its recurrence count bumped, or a new one.

        Deliberately does NOT touch `revised_refusals`: this method records that
        a fault was SEEN, and the repeat guard meters what the loop DID about it
        (`note_refusal_revise`). `replace` carries the field over unnamed, which
        is what keeps a bump from blanking the guard's memory of an episode while
        claiming to update it.

        WHAT `replace` CARRIES OVER IS THE LIST OBJECT, not a copy — `bumped` and
        `existing` share one. That is why `note_refusal_revise` REBINDS the field
        to a new list rather than appending to it, and why anything else writing
        this field must do the same: an in-place `.append` here would write
        through both records at once, and through whichever other record a caller
        still holds.
        """
        existing = self.find_open(task_id, code, phase)
        if existing is not None:
            from dataclasses import replace
            bumped = replace(
                existing,
                recurrences=existing.recurrences + 1,
                last_seen_at=now,
                question=question,
                detail=detail,
            )
            self.save(bumped)
            return bumped
        fresh = Blocker(
            id=self.next_id(task_id), task_id=task_id, kind=kind, code=code,
            question=question, detail=detail, phase=phase, created_at=now,
            last_seen_at=now, session_id=session_id,
        )
        self.save(fresh)
        return fresh

    def note_refusal_revise(self, blocker_id: str, fingerprint: str) -> "Blocker":
        """Spend this refusal identity's one revise allowance, on this record
        (halt-04). THE ONLY WRITER of `Blocker.revised_refusals`.

        Called at the moment the loop COMMITS to a self-issued revise — before
        the round is queued, never after it returns. A process that dies between
        the two leaves the attempt spent and the round unrun, which is the safe
        direction: the next occurrence of that refusal is set aside rather than
        revised a second time. The reverse order would let a crash refund an
        attempt the loop had already decided to make.

        An EMPTY fingerprint RAISES rather than being appended. `refusal_identity`
        returns `""` for a refusal with no text, and a stored `""` would be a
        meter entry that every other textless refusal matched — a task set aside
        on two absences rather than on a repeat. Its one caller refuses a
        textless refusal a step earlier, so reaching here with one is a bug and
        is reported as one.
        """
        if not fingerprint:
            raise StateError("a refusal with no identity cannot be metered")
        blocker = self.load(blocker_id)
        if blocker is None:
            raise StateError(f"no blocker with id '{blocker_id}'")
        blocker.revised_refusals = [*blocker.revised_refusals, fingerprint]
        self.save(blocker)
        return blocker

    def refusal_revises(self, task_id: str, fingerprint: str) -> int:
        """How many self-issued revises this task has already been given for THIS
        refusal identity — halt-04's meter, read by
        `orchestrator._refusal_revise_budget`.

        Keyed on the refusal rather than on its code, which is the whole of the
        repeat guard: the same complaint twice is a loop and must stop, while a
        different complaint under the same code is feedback the agent has never
        been given. `refusal_identity` folds the code into the digest already, so
        this is blind to code and to phase without needing to be told either —
        the same phase-blindness `open_recurrences` has, and for the same reason.

        **Counts CLOSED records as well as open ones.** An operator answering the
        blocker, `archive_stale` retiring its session and `close_recovered` all
        end the episode, and a meter that forgot the attempt at that moment would
        hand the identical refusal a fresh allowance every time a record went
        away. Nothing in this package deletes a blocker record, so the memory is
        permanent by construction rather than by a record being left open.

        An empty `fingerprint` counts NOTHING and returns 0 — `""` may never
        match `""`. Its caller refuses a textless refusal outright rather than
        metering it, so that 0 can never license one.

        Reads through `all_blockers`, so a corrupt record RAISES rather than
        reading as "nothing spent", which would be the fail-open answer: a
        further revise licensed by evidence that could not be read.
        """
        if not fingerprint:
            return 0
        return sum(
            blocker.revised_refusals.count(fingerprint)
            for blocker in self.all_blockers()
            if blocker.task_id == task_id
        )

    def archive_stale(self, blocker_id: str, reason: str) -> "Blocker":
        """Close a blocker that belongs to a RETIRED session, recording a
        machine reason rather than an operator answer.

        Deliberately not `resolve()`: that writes `answer`, which means "the
        operator responded to this question", and using it to clear a dead
        blocker would fabricate exactly the confirmation
        `cli._RESOLUTION_PRECONDITIONS` exists to demand. A blocker whose
        session no longer exists was never answered — it was abandoned, and
        the record should say so. `reason` is required and non-empty so an
        archival can never be a silent delete.
        """
        return self._close_without_answer(blocker_id, reason, "archive_stale")

    def close_recovered(self, blocker_id: str, reason: str) -> "Blocker":
        """Close a blocker whose fault AUTONOMOUS MODE ACTUALLY RECOVERED FROM
        — the loop retried, a step then completed, and the condition is gone.

        A sibling of `archive_stale` rather than a call to it, because the two
        record different facts and an operator reading `archived_reason` has to
        be able to tell them apart: one says the question was abandoned with a
        session, this one says the loop answered it by succeeding. Both refuse
        to write `answer`, for the same reason — that field means an operator
        responded, and forging it would fake exactly the confirmation
        `cli._RESOLUTION_PRECONDITIONS` exists to demand.

        Closing is what makes the retry budget PER EPISODE. The budget is read
        off `Blocker.recurrences` for the open record of this
        (task, code, phase), so a record left open after a recovered fault
        would hand the next, unrelated occurrence a budget that was already
        spent — a login expiry today silently costing next week's its retries.
        A close that never happens (the process dies mid-retry, the store
        cannot be written) leaves the budget spent, which is the safe
        direction: the loop retries less, never more.
        """
        return self._close_without_answer(blocker_id, reason, "close_recovered")

    def _close_without_answer(self, blocker_id: str, reason: str, caller: str) -> "Blocker":
        """Shared body of `archive_stale` / `close_recovered`: resolve a
        blocker with a machine reason and no operator answer. `reason` is
        required and non-empty so neither can ever be a silent delete."""
        if not reason.strip():
            raise StateError(f"{caller} requires a non-empty machine reason")
        blocker = self.load(blocker_id)
        if blocker is None:
            raise StateError(f"no blocker with id '{blocker_id}'")
        if blocker.resolved_at is not None:
            raise StateError(
                f"blocker '{blocker_id}' is already closed (at {blocker.resolved_at})"
            )
        blocker.resolved_at = utcnow_iso()
        blocker.archived_reason = reason
        self.save(blocker)
        return blocker

    def all_blockers(self) -> list[Blocker]:
        """Every blocker on disk, open or resolved, oldest id first per
        task (lexicographic filename order — ids are zero-padded, so this is
        also chronological within a task)."""
        if not self.directory.exists():
            return []
        loaded = [self.load(path.stem) for path in sorted(self.directory.glob("blk-*.json"))]
        return [b for b in loaded if b is not None]

    def open_blockers(self) -> list[Blocker]:
        return [b for b in self.all_blockers() if b.resolved_at is None]

    def open_recurrences(self, task_id: str, code: str) -> int:
        """How many times this (task, code) has been recorded across every OPEN
        record, whatever phase each was raised in. 0 when none is open.

        THE retry budget's meter for `orchestrator._to_needs_user`, and
        deliberately blind to phase where `find_open` is not. `record` keys on
        (task, code, phase) because that is the right identity for an operator
        question — the same fault in `submitting` and in
        `submission_unconfirmed` genuinely reads differently. It is the wrong
        identity for a BUDGET: a fault that migrates one phase along would
        arrive at a fresh record and buy itself a second full allowance, which
        is exactly the unbounded retry the budget exists to prevent. Summing is
        the conservative direction — it can only ever spend the allowance
        faster.

        Reads through `open_blockers`, so a corrupt record RAISES here rather
        than reading as "nothing open", which would be the fail-open answer: it
        would hand the caller a budget of zero-spent and license a retry on
        evidence that could not be read.
        """
        return sum(
            b.recurrences for b in self.open_blockers()
            if (b.task_id, b.code) == (task_id, code)
        )

    def open_blockers_by_severity(self) -> list[Blocker]:
        """Every OPEN blocker, most urgent first (`primary_sort_key`).

        Deliberately a SECOND method rather than a reordering of
        `open_blockers`: callers that want the whole set — `find_open`,
        `open_task_ids`, `cli._reconcile_retired_blockers` — do not care
        about order, and changing the order under them would be churn with
        no reader. Nothing is filtered here; ranking a primary must never
        cost the operator sight of the rest."""
        return by_severity(self.open_blockers())

    def primary_blocker(self) -> Blocker | None:
        """THE open blocker — the most severe, most recent one, or `None`
        when nothing is open.

        The single answer to "which blocker is this loop stuck on?". With
        exactly one open it is that one, which is the overwhelmingly common
        case and identical to reading the set's only element; with several
        it is decided by `primary_sort_key`, never by directory order.
        Callers that name it MUST still say how many others are open —
        selecting a primary is not permission to hide the rest."""
        ordered = self.open_blockers_by_severity()
        return ordered[0] if ordered else None

    def open_task_ids(self) -> set[str]:
        """Every task id named by at least one OPEN blocker.

        The question `cli._reconcile_unblocked_tasks` asks of this store — "is
        there still anything to answer about this task?" — and the reason a
        `blocked` status is allowed to persist. A task that is absent from this
        set has nothing left justifying its quarantine, so leaving it out of
        `next_ready()` is a split brain rather than a decision (blk-01).

        EVERY KIND counts, deliberately unlike `cli._reconcile_retired_
        blockers`' `task_fatal` allowlist. That sweep CLOSES records, so it has
        to prove a record is closeable; this one closes nothing and only ever
        decides whether to keep a task out of the queue, so the conservative
        direction is the opposite: a `loop_fatal` record naming a task is still
        an open question about it, and keeping the task blocked while it stands
        costs a delay, where releasing it early costs a dispatch nobody
        authorised. `NO_TASK` needs no special case — `tasks._ID_RE` forbids
        parentheses, so `"(loop)"` can never be a real task id and can never
        match one.

        Reads through `open_blockers`, so a corrupt record RAISES here too
        rather than reading as "nothing open" — which would be exactly the
        wrong answer for a caller about to decide a quarantine is over.
        """
        return {b.task_id for b in self.open_blockers()}

    def resolve(self, blocker_id: str, answer: str) -> Blocker:
        """Mark `blocker_id` resolved with the operator's `answer`. Refuses
        an unknown id or one that is already resolved — resolution is a
        one-way, one-time action, never silently overwritten by a second
        `answer` call."""
        blocker = self.load(blocker_id)
        if blocker is None:
            raise StateError(f"no blocker with id '{blocker_id}'")
        if blocker.resolved_at is not None:
            raise StateError(
                f"blocker '{blocker_id}' is already resolved "
                f"(at {blocker.resolved_at}, answer: {blocker.answer!r})"
            )
        blocker.resolved_at = utcnow_iso()
        blocker.answer = answer
        self.save(blocker)
        return blocker
