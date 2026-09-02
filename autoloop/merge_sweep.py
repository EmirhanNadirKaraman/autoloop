"""Sweep the backlog of published-but-unmerged branches into the base.

`auto_merge.py` reacts to ONE completion: the task it just saw publish gets
merged, and its deferral queue retries the ones it saw earlier. Neither
covers a branch that was published before any of that existed, or published
by a process that died before it could integrate anything. Those branches
have no event left to react to, and nothing ever looks for them again.

On **2026-08-06 seven completed tasks were published and unmerged at the same
time** — auto-08, auto-12, brw-01, brw-07, inbox-09, rt-10, rt-11 — while the
base was still at d2d4d6b. Nothing surfaced it. It took a hand-written
`git ls-remote` loop to notice, and two of the seven were fixes for failures
the loop was still hitting while their code sat on branches nobody had
pulled. This module is the thing that would have noticed.

## What it does NOT reimplement

Every rule about *how* a branch reaches the base already exists in
`auto_merge.AutoMerger.attempt`: the merge window gate, the dirty-checkout
refusal, the moved-remote-base check, the merge itself, the four-part
verification that the merge really happened, the conflict abort, and the push.
This module CALLS that method once per branch. It contributes exactly four
things `attempt` has no way to know about, all of which are properties of the
SEQUENCE rather than of any one merge:

1. **Enumeration** — which branches are outstanding at all.
2. **Order** — oldest publication first.
3. **Stopping** — the whole sweep halts at the first branch that does not land.
4. **Freshness** — `attempt` cannot tell whether the publication evidence it
   was handed came from a moment ago or from before the merges that preceded
   it, because it only ever sees one task. The sweep can, so the sweep is where
   the memoized answer is invalidated per branch (`_reconfirm`).

A second implementation of the merge rules that drifted by one case is the
same failure `_merge_window_blockers` exists to prevent; see `auto_merge.py`'s
own note about calling the gate rather than copying it.

## Unmerged-ness is decided by git ancestry, and by nothing else

`merge-base --is-ancestor <candidate> <base HEAD>`. Not the task's status, not
the branch's name, not whether a `MergeDeferral` exists for it. Status and
name are precisely what made the 2026-08-06 backlog invisible: every one of
those seven tasks was `completed` with a plausibly-named branch on origin, and
both facts are equally true of a branch that landed in the base an hour ago.
Only ancestry distinguishes them.

A candidate the checkout cannot resolve answers "not an ancestor" rather than
raising, and that is not merely fail-open: a commit that is an ancestor of
HEAD is by definition in this object database, so an unresolvable one is
provably not integrated here. It goes to `attempt`, which fetches it from the
task's own worker repo (`_ensure_object`) before deciding anything.

The registry is still read — the sweep only touches COMPLETED tasks, matching
`AutoMerger.attempt`'s own refusal to merge over a task an operator has
quarantined — but it is used to narrow the candidate set, never to conclude
that something is already merged.

## "Could not look" is never "nothing to merge"

A completed task has THREE possible answers here, not two: its branch is in the
base, its branch is outstanding, or **the sweep could not tell**. The third has
to survive into the exit code, or it silently collapses into the first — the
2026-08-06 invisibility rebuilt one layer up, inside the tool written to end it.

`SweepResult.unresolved` is that third answer. Four states reach it, and what
they share is that being COMPLETED implies a confirmed publication
(`_mark_task_completed` fires on nothing else) — so for each of them there is
very likely a branch out there, and this module cannot name it:

* **The remote does not confirm the branch.** The ref was deleted or
  force-moved after completion, or the remote is unreachable right now.
  `_candidate_publication` cannot tell those apart and is not asked to: an
  unverifiable answer is not an answer.
* **The execution record cannot be READ** — a torn file, an I/O error, a
  permission failure. Named since 2026-08-15; before that it was logged and
  then dropped, so a sweep whose only completed task had a corrupt record
  reported `nothing_to_do` and exited 0 having inspected nothing.
* **There is no live record, and the NEWEST archived one does not prove the
  work landed** — including when the archived copies cannot be put in
  generation order at all, or the newest of them will not load.
  `_retired_publication_is_integrated` explains why the archive is read here
  when the merge window deliberately ignores it; the reading itself is
  `execution_record_ancestry`, which since witness-01 (2026-08-27) is shared
  with `dashboard.registry_disagreements` so the report and the sweep cannot
  reach different verdicts about the same record.
* **The record loads but names no candidate**, and nothing else names one
  either. Completion implies a candidate, so such a record cannot be describing
  the publication completion implies. Since merge-09 the archive is asked for
  the sha before this is concluded — a re-dispatched stub is exactly this shape
  and the retirement it replaced named its own sha in the FILENAME (see "a
  candidate the 2026-08-27 extraction rewrote").

None of the four is ATTEMPTED: there is nothing here this module is allowed to
merge, and inventing a branch out of a record's own claim is the fail-open
reading `_candidate_publication` exists to refuse.

And since 2026-08-15 none of the OTHER branches is attempted either. **Any
unresolved task found during enumeration makes the whole invocation
non-mutating** (`HELD`), because an unjudgeable task is not provably independent
of the candidates that ARE judgeable:

    publish A; cut B from A; A's ref is deleted (or its record goes unreadable)
    while B's is still confirmed.

A is excluded — correctly, nothing here may merge it. But B is a DESCENDANT of
A, so merging B makes A an ancestor of HEAD anyway, without A's publication ever
having been confirmed. This module exists precisely because a later branch is
allowed to build on an earlier one (see the order section), so that shape is
ordinary rather than exotic, and it is worse for the unreadable-record case: a
record nobody can read cannot even be checked for the relationship. Skipping the
unresolved entry and sweeping on therefore integrates, transitively, exactly the
work the skip refused to integrate directly.

## What is NOT enumerated: a task recorded as shipped elsewhere

`TaskState.SHIPPED_ELSEWHERE` (ship-01, 2026-08-23) means the task's work is in
the base under ANOTHER task's commits. Such a task never had a branch, never had
an execution record, and has nothing here to merge — so `_backlog` does not
enumerate it at all. That falls out of the enumeration rule already written
above (only `TaskState.COMPLETED` is a candidate), and it is stated here rather
than left implicit because the alternative was tried and is exactly what this
module must not do: completing those tasks to tidy the registry would enumerate
five tasks whose records name no candidate, and a record naming no candidate is
`unresolved` — so every future sweep would be HELD, on work that is already in
the base.

It is emphatically NOT a fifth `unresolved` reason either. The four reasons in
that list share one property: being COMPLETED implies a confirmed publication,
so there is very likely a branch out there this module cannot name. A
shipped-elsewhere record makes the opposite claim, and makes it with evidence
(`Task.shipped_commits`), so there is no branch to be unaccounted for. Skipping
it therefore costs no coverage — nothing is being rounded down to "nothing to
merge", which is the rule the rest of this docstring exists to protect. What
DOES check that claim is `dashboard.shipped_elsewhere_states`, which re-asks git
whether each recorded commit is still an ancestor of the base head and reports a
record that has stopped holding as a disagreement. Nothing in this module reads
or acts on those commits.

The genuinely-completed rule is unchanged in every respect: a completed task
that cannot be judged still makes the whole invocation non-mutating.

Being unresolved is per-task, but the safe response is per-INVOCATION, and
deliberately so. The alternative — excluding every candidate that descends from
an unresolved one — needs the ancestry of a commit the sweep may be unable to
name or resolve at all, which is the same unanswerable question one step along.
Nothing has been mutated at that point, so holding costs only a delay.

**The cost is real and accepted**: one stale unjudgeable task blocks every sweep
until an operator deals with it. That is the intended trade — the branches this
module merges have already sat unmerged for days, and the report names what to
fix — but it is a cost, not a free win. `merge_sweep_held` names both the
unjudgeable tasks and the untouched branches so the operator can see what the
one is holding up.

Every one of them makes the run not-clear. `SweepResult.is_clear` is false
whenever `unresolved` is non-empty, so `merge-backlog` exits 1 and the startup
hook prints rather than staying quiet. Exit 0 means "I looked, and there is
provably nothing outstanding", never "I could not look". Startup still only
REPORTS: a held sweep does not stop the loop from running, exactly as a deferred
or stopped one does not.

A publication that stops being confirmed MID-SWEEP is a different answer and
keeps its own (`UNCONFIRMED`, below): by then branches have already been merged
and pushed, so the honest report is where the sweep got to, not a pretence that
nothing happened.

## Enumeration evidence is not mutation authority

`seen` memoizes CONFIRMED publications so a long-running command does not
re-ask the remote about the same ref forever (`cli._candidate_publication`
documents the cache and why negatives are never cached). Sharing one such cache
between the ENUMERATION and the MERGES would quietly change what it means:
branch N would be merged on an `ls-remote` taken before branches 1..N-1 were
attempted, so a delete or force-move in between — another operator, a
`release`, a CI job — is hidden by the positive cache instead of caught by it.
A seven-branch sweep is minutes of merging and pushing; that window is real.

So each candidate's own key is EVICTED from `seen` immediately before its own
merge (`_reconfirm`), which forces one live `ls-remote` for that branch at the
moment it matters; re-adding it on success means `AutoMerger.attempt`'s own
publication check (its step 3) costs nothing on top. A ref that no longer
carries the reviewed candidate yields `UNCONFIRMED`, which is not in
`_CONTINUE_ON`: that branch is not merged, and neither is anything after it.

That is deliberately harsher than the enumeration-time version of the same
finding, which is merely named and passed over. A ref that changed WHILE the
sweep was running means something else is mutating the remote right now, and
that invalidates the whole enumeration rather than one entry of it. The other
way this answer comes back False — a transient `ls-remote` failure partway
through — stops the sweep for the plainer reason: mid-sweep is the one moment
when "the remote stopped answering" and "the ref moved" are indistinguishable
AND the base has already changed under the remaining candidates, so fail-closed
means halt. Nothing is lost either way; the next run re-enumerates from git.

## Order: oldest publication first

Attempting the backlog in arbitrary order manufactures conflicts that do not
really exist. If branch B was cut after branch A and touches what A touched,
merging B first collides with A's changes on the base; merging A first makes
B apply cleanly. The order is therefore publication order:

* `TaskExecution.published_at` when the record carries one.
* A record with an EMPTY `published_at` predates the field (added 2026-08-15,
  see `worktask.py`), so it is older than every record that has one and sorts
  ahead of all of them. That is not a guess — it is what an absent field
  means here, and it happens to be exactly the 2026-08-06 backlog this module
  was written for.
* Within that older group, the candidate commit's own committer timestamp
  breaks the tie, since a branch cut from another branch commits later than
  the one it builds on. Unreadable timestamp sorts first; ties break on task
  id so two runs over the same backlog always attempt it in the same order.

Order is a heuristic, and it is allowed to be, because **stopping makes a
wrong order safe**: a mis-ordered pair conflicts, the sweep aborts that merge
and halts with the base byte-identical, and the operator resolves it. A wrong
order costs a stalled sweep. It never costs a corrupted base.

## Stop at the first branch that does not land

Not just conflicts. `AutoMerger._merge` deliberately does NOT undo a merge
that failed verification (`reset` is absent from the git whitelist by design),
so continuing past a `failed` outcome would stack a second merge onto a head
nobody understands. A deferral means a precondition the whole sweep shares
(the remote base moved, the checkout went dirty) has stopped holding. All of
them halt the sweep, the remaining branches are left untouched and NAMED in
the transcript, and the operator gets one situation to reason about instead of
a half-swept backlog with one branch aborted somewhere in the middle.

One deferral is per-branch rather than sweep-wide and still stops everything,
deliberately: `AutoMerger._ensure_object` failing — a candidate the checkout
cannot resolve and whose recorded worker repo is gone. This gateway has no
policy-legal way to fetch it from the remote (`fetch_object` takes a local path
by design), so that branch cannot be integrated here at all, and every sweep
from now on will stop at it. That is the intended report, not an oversight: the
branches behind it may well build on it, and a tool quietly working around a
branch it can never merge is how a backlog becomes invisible again. The recovery
is an operator `git fetch` of the branch followed by a merge by hand: the
candidate is then an ancestor of HEAD, so the next sweep skips it silently and
carries on with the rest. NOT `release` — that refuses anything not in progress
(`TaskRegistry.release`), and every task the sweep enumerates is completed, so
suggesting it here would send the operator at a command that cannot run.
`merge_sweep_stopped` names the branch and the remainder.

## A stop is not automatically a restoration

Stopping keeps the sweep from stacking a second merge onto a head nobody
understands. It does NOT, by itself, mean the checkout is back where it started
— and until 2026-08-15 both this module and its report assumed it did.

`AutoMerger.attempt` has two outcomes that leave the base MOVED:

* a merge that ran and then failed VERIFICATION (`auto_merge.FAILED`) — the
  resulting head fails one of the ancestry checks, or the tree is unexpectedly
  dirty afterwards. The merge commit is in the checkout and is deliberately
  not undone, because `reset` is absent from the git whitelist by design.
* a merge that verified and whose PUSH was then refused or failed
  (`auto_merge.DEFERRED`, logged `auto_merge_push_refused`). The base moved
  locally and never reached the remote — which is what a base branch listed in
  `protected_branches` produces every single time.

The second is exactly the one that must not be classified by outcome slug,
because `DEFERRED` otherwise means "a precondition was not met and nothing was
touched". So the answer is not read off the slug at all: the checkout is PROBED
immediately before each attempt and again the moment one does not land, and
"the base is exactly as it was" is claimed only when those two observations
match (`_unreconciled` / `SweepResult.is_reconciled`). A probe that could not
read the checkout is not a match either — "could not look" is not "nothing
moved", the same rule the unresolved section applies to enumeration.

That distinction is what the STARTUP hook needs. Reporting a stop and then
dispatching ordinary roadmap work onto the resulting checkout defeats the whole
reason for stopping: with no policy-legal undo available, the loop would build
its next task on a head nobody verified, or push work stacked on a merge the
remote has never seen. `cli._sweep_backlog_on_startup` therefore reports whether
it is safe to carry on, and `_cmd_run` refuses to enter `_run_locked` when it is
not. A conflict that aborted cleanly, a shut gate, a dirty-checkout refusal, a
publication that changed before its merge and a HELD enumeration all mutate
nothing, so every one of them still reports-and-continues exactly as before —
the branches they are complaining about have already waited days, and refusing
to start the loop over them would be the strictly worse failure.

## The gate defers the whole sweep, never part of it

`cli._merge_window_blockers` is checked ONCE before the first merge. Shut →
nothing is attempted at all. Letting each branch discover the shut gate for
itself would write one `MergeDeferral` per branch and log N deferrals for one
condition, which is the "part of it" this exists to avoid. `attempt` still
re-checks the gate per branch, and that check stays: it is the race guard for
a window that shuts mid-sweep.

**And at `lanes > 1` it is the race guard for the re-review obligation too**
(conc-03, docs/AUTOLOOP.md Decision 6). Each merge inside a sweep moves the base
for every candidate that is not it, so the obligation cannot be computed once at
the start: a candidate bound to the head before branch 1 is bound to a DIFFERENT
head before branch 2, and one carried forward onto branch 1's merge is bound to
that. Because the obligation is minted by the same per-branch gate call
`attempt` already makes, a three-branch sweep evaluates it three times, marks
before each merge and carries forward after each one — the sweep contributes
nothing to it except the sequence, exactly as it contributes nothing to the
merge rules themselves.

The all-or-nothing property is untouched by all of that. The sweep-wide gate
still runs first and still defers everything or nothing; a merge whose
obligations this process cannot discharge DEFERS (`AutoMerger` fails closed on a
missing carry-forward), which stops the sweep at that branch through
`_CONTINUE_ON` exactly as any other deferral does; and a carry-forward that
REFUSES parks the task it is about without changing the merge's outcome, so it
neither halts the sweep nor merges anything extra.

## No state of its own

There is no sweep queue on disk. The work-list is re-derived from git ancestry
on every invocation, so a sweep that stopped halfway simply re-enumerates what
is left the next time it runs — idempotent by construction, and incapable of
the stale-record drift that `MergeDeferralStore` needs `attempts` and
`last_seen_at` to survive. A branch this module merges IS recorded, in the
transcript, by `AutoMerger`'s own `auto_merge_pushed` entry.

Which makes the TRANSCRIPT the only history of what this module has decided,
and since sweep-01 (2026-08-25) that history is read back:
`health.held_merge_sweep` turns the terminal entries into "held by X, with Y
queued behind it, for Z hours", so a hold reaches an operator through
`health --json` instead of being logged hourly and read by nobody — measured,
on this repository: `audit-0001` held the sweep for 225.8 hours across 108
consecutive sweeps while five approved tasks waited, and `health` mentioned
merges zero times. For that to be decidable EVERY invocation has to write
exactly one terminal entry, which is why `SWEEP_IDLE_EVENT` exists: a clear
sweep used to write nothing, so "still held" and "held, then fixed" left the
same evidence and a reader could only ever fail one way or the other. Per-branch
silence is unchanged. Nothing in what this module MERGES depends on any of it —
the refusal to sweep past work it cannot judge is exactly as it was, and making
that refusal visible is the whole change.

## A candidate the 2026-08-27 extraction rewrote

`git filter-repo --path autoloop/` rewrote every commit in this repository and
migrated NO execution record, so 133 completed tasks still name a
`candidate_sha` from the OLD history — an object that is not in this object
database and never will be. Each reported `origin/refs/heads/autoloop/<id> does
not exist` and the backlog was held for 90 hours on them. The ref message was a
symptom: the record names a commit no ref anywhere can carry.

Two repairs were tried and reverted. Pushing the 133 refs (2026-08-30) moves the
answer to `is at <new>, not the candidate` — the same hold one step along, since
the refs carry the REWRITTEN commits. An ancestor walk from each branch tip, the
same day, put 8 branches on ANOTHER task's commit: near-in-history is not
is-the-work.

What answers is `docs/extraction/commit-map.tsv`, filter-repo's own old->new
map, vendored as evidence (`docs/extraction/PROVENANCE.md`). `_recover` consults
it, and three rules in one fixed order, for a candidate this checkout does not
hold:

1. **The record's own sha**, or — when the live record names none, which is what
   a re-dispatched stub leaves — the sha in the newest ARCHIVED filename that
   carries one (`<id>-reconciled-as-<sha>-<stamp>.json`,
   `<id>-merged-as-<sha>-<stamp>.json`). The archived copies are scanned
   newest-first for a label the pattern applies to, because a LATER retirement
   under some other label (`-report-recovered-by-operator-<stamp>`) says nothing
   about the sha and must not hide the one that does — `audit-0001` exactly.
2. **The commit map**, keyed by that sha. A row mapping to a real commit is
   judged by ancestry exactly as if the record had named it; a row of forty
   zeros means the rewrite PRUNED the commit — it touched no path under
   `autoloop/`, so there is nothing here to merge and nothing that should exist.
   **A row that is present is FINAL**: mapped-but-not-an-ancestor is
   `unresolved`, never rescued by rule 3.
3. **A merge commit on the base head's first-parent chain** whose SUBJECT is
   `Merge task <id> (<sha>)` — `AutoMerger._merge`'s own wording, which is the
   loop's record of having integrated that task. Last resort, for a candidate
   the operator discarded, and reached only after the publication check has
   already failed: a task that was merged once and RE-dispatched has such a
   commit in the base while its new branch is genuinely outstanding, and reading
   the old merge as an answer about the new branch would skip it.

Everything here is gated on the candidate not being an object in this
repository, so a record git can resolve is judged exactly as it was before —
including the ordinary case of a candidate this checkout has simply never
fetched, which still goes to the remote and then to `_ensure_object`.

FAIL CLOSED, everywhere. A map that is missing, unreadable or empty resolves
NOTHING; an abbreviated sha matching more than one row resolves nothing; a
first-parent walk that could not be completed resolves nothing; and no rule here
clears a task on a written claim, on a task id appearing in a commit BODY, or on
an ancestor walk from a branch tip. Each of those leaves the task `unresolved`,
which is what it was before this existed.

Nothing here MERGES. Every verdict is either "the branch is accounted for in the
base", "the commit does not exist in this repository at all", or "still
unresolved" — no candidate reaches `AutoMerger.attempt` through these rules,
because the commit they are about is one nothing can merge from.

## Failure discipline

Identical to `auto_merge.py`'s, and for the same reason: this runs at startup,
before the loop has done anything. An integration problem must never stop a
run from starting, so every failure swallows to a transcript entry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import auto_merge
from .auto_merge import AutoMerger
from .config import AutoloopConfig
from .errors import GitError, StateCorruptError
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .tasks import TaskState
from .transcript import TranscriptLogger
from .worktask import TaskExecutionStore

#: Outcome slugs, used verbatim as the tail of the transcript entry type
#: (`merge_sweep_<slug>`), so a log grep and a test assertion name the same
#: thing — the same convention `auto_merge.py` follows.
SWEPT = "swept"                  # every outstanding branch reached the base
NOTHING_TO_DO = "nothing_to_do"  # no completed, published, unmerged branch
DEFERRED = "deferred"            # the gate was shut: NOTHING was attempted
HELD = "held"                    # a task could not be judged: NOTHING was attempted
STOPPED = "stopped"              # a branch did not land; the rest are untouched
DISABLED = "disabled"            # policy.auto_merge_enabled is false
FAILED = "failed"                # the sweep itself could not run

#: A PER-BRANCH outcome this module mints itself rather than getting back from
#: `auto_merge`: the remote no longer confirmed the candidate at the moment its
#: own merge was about to run. Deliberately not an `auto_merge` slug — nothing
#: reached `AutoMerger.attempt`, so claiming one of its outcomes would say the
#: merge machinery decided something it never saw.
UNCONFIRMED = "publication_unconfirmed"

#: The transcript entry each TERMINAL outcome writes. Named constants rather
#: than literals at the emit sites because these are read BACK:
#: `health.held_merge_sweep` reconstructs "is this backlog held, by what, with
#: what queued behind it, and for how long" from nothing but these entries, and
#: a slug renamed at one end only would leave that reader watching for an event
#: nobody writes — silently, which is this module's own failure mode one layer
#: up. The literal strings are pinned by a test for the same reason: the 110
#: entries already in the transcript on 2026-08-25 carry these exact names, and
#: a rename both ends agreed on would still stop the history being readable.
SWEEP_HELD_EVENT = "merge_sweep_held"
SWEEP_COMPLETED_EVENT = "merge_sweep_completed"
SWEEP_DEFERRED_EVENT = "merge_sweep_deferred"
SWEEP_STOPPED_EVENT = "merge_sweep_stopped"

#: What an invocation with nothing to merge writes — the one entry that makes
#: the SEQUENCE of sweeps decidable (sweep-01, 2026-08-25). Until it existed a
#: clear sweep wrote nothing at all, so "still held" and "held, then fixed and
#: clear ever since" left identical evidence, and any reader of the entries
#: above had to keep reporting a hold that might have ended weeks ago. It
#: carries `unresolved` because a nothing-to-do invocation can still be
#: unjudgeable: `_backlog` records unresolved tasks and then finds no candidate
#: to queue behind them, which is a backlog that cannot drain with an empty
#: queue — not a clear one, as `is_clear` has always said.
#:
#: This is a LOG line and nothing else. It changes no enumeration, no order, no
#: gate and no merge; a sweep that writes it merged nothing before it and will
#: merge nothing because of it.
SWEEP_IDLE_EVENT = "merge_sweep_nothing_to_do"

#: Terminal entries that PROVE the enumeration found nothing it could not judge.
#: All three are written PAST the hold check, which returns before them — so any
#: one of them, seen after a run of held sweeps, ends that run. `merge_sweep_
#: error` is deliberately in neither set: it is also written per-branch from
#: `_attempt` (followed by `merge_sweep_stopped`), so treating it as terminal
#: would misread a composite run, and a crashed enumeration proves nothing about
#: a hold either way. Ignoring it keeps the hold reported, which is the closed
#: direction.
SWEEP_CLEARED_EVENTS = (
    SWEEP_COMPLETED_EVENT,
    SWEEP_DEFERRED_EVENT,
    SWEEP_STOPPED_EVENT,
)

#: One line per completed task a rule in "a candidate the 2026-08-27 extraction
#: rewrote" answered for. Deliberately NOT silent, unlike the ordinary
#: already-integrated case: these tasks held every sweep for 90 hours, and an
#: operator repairing the records needs to see which rule answered for which
#: task and on what sha. It is a LOG line and nothing else — the sweep merges
#: nothing because of it.
SWEEP_RECOVERED_EVENT = "merge_sweep_candidate_recovered"

#: The only two per-branch outcomes the sweep continues past. Everything else
#: — conflict, failed verification, deferral, a publication that stopped being
#: confirmed, an unexpected skip — halts it. `already_integrated` is a success:
#: merging an earlier branch can carry a later one in with it, which is exactly
#: what building on it means.
_CONTINUE_ON = (auto_merge.MERGED, auto_merge.ALREADY_INTEGRATED)


@dataclass(frozen=True)
class SweepCandidate:
    """One completed task whose published branch is not in the base yet."""

    task_id: str
    candidate_sha: str
    #: Where the enumeration confirmed the candidate, kept so the merge can
    #: re-ask the SAME ref without reloading the record. See `_reconfirm`.
    remote: str
    dest_ref: str
    #: `(group, timestamp, task_id)` — see the module docstring's order section.
    #: `group` is 0 for a record with no `published_at` (older than any record
    #: that has one) and 1 otherwise.
    order: tuple


@dataclass
class SweepResult:
    """What one sweep did. Returned for the CLI to print and for tests to
    assert on; the transcript is what an operator greps after the fact."""

    outcome: str
    #: Branches that reached the base, in the order they were attempted.
    merged: list[str] = field(default_factory=list)
    #: Branches still outstanding: the gate shut before any of them was tried,
    #: a task could not be judged and held the whole invocation (`HELD`), or the
    #: sweep stopped — in which case the one it stopped ON leads the list, since
    #: it did not land either. Named so "the rest were left alone" is a checkable
    #: claim rather than an absence.
    pending: list[str] = field(default_factory=list)
    #: `(task_id, reason)` for a completed task the sweep could not JUDGE —
    #: an unconfirmed publication, an unreadable record, a retired record whose
    #: work cannot be shown to have landed, a record naming no candidate. Not
    #: attempted, not fatal, and never counted as clear. See the module
    #: docstring's "could not look" section for why all four belong together.
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    #: The branch that halted the sweep, and the outcome it got — an
    #: `auto_merge` slug, or `UNCONFIRMED` when it never reached the merger.
    stopped_on: str = ""
    stopped_outcome: str = ""
    #: The gate's reasons, when the whole sweep was deferred.
    reasons: list[str] = field(default_factory=list)
    #: HEAD when this invocation began and when it returned, `""` for either
    #: one the checkout would not answer. Kept so "did this leave the base
    #: somewhere else?" is a comparison an operator can check rather than an
    #: inference from the outcome slug — and so the report can name both ends.
    base_before: str = ""
    base_after: str = ""
    #: WHY the checkout cannot be claimed to be where the stopping attempt
    #: found it — a moved HEAD, a changed working tree, or a probe that could
    #: not read either. `""` means it provably is, which is the only state in
    #: which anything downstream may treat this checkout as ordinary. See the
    #: module docstring's "a stop is not automatically a restoration".
    unreconciled: str = ""

    @property
    def is_reconciled(self) -> bool:
        """Is the checkout provably in a state this module finished putting it
        in — either untouched, or moved only by merges that were verified AND
        pushed?

        Deliberately separate from `is_clear`: they answer different questions
        and have different consequences. `is_clear` is about the BACKLOG (is
        anything still unmerged) and drives the exit code; this is about the
        CHECKOUT (is it safe to keep working in) and drives whether the startup
        hook lets the loop run at all. A stopped sweep is never clear, but it is
        usually reconciled — a conflict aborts back to the exact pre-merge head
        — and conflating the two would either block every startup after a
        conflict or let one through after an unverified merge.
        """
        return not self.unreconciled

    @property
    def is_clear(self) -> bool:
        """Is there provably nothing left unmerged? The ONE definition both
        callers use for their exit code and their output.

        An unresolved task counts as not-clear, and that is the whole point of
        this being a property rather than an outcome comparison. `unresolved`
        holds every completed task the sweep could not JUDGE — an unverifiable
        publication, a record it could not read, a retired record whose work it
        cannot show landed. All of them mean "I could not look", and reporting
        one of those runs as `nothing_to_do` would say "I looked, the backlog is
        clear" instead: the 2026-08-06 invisibility rebuilt one layer up, in the
        tool written to end it.

        `HELD` never reaches the first test either, so it needs no case of its
        own: it exists only when `unresolved` is non-empty. An unreconciled
        checkout cannot reach it either today — only `STOPPED` and `FAILED`
        carry one — but it is tested for anyway, because "provably nothing left
        unmerged" is not a claim to make about a base nobody can explain.
        """
        return (
            self.outcome in (SWEPT, NOTHING_TO_DO)
            and not self.unresolved
            and not self.unreconciled
        )


class BacklogSweeper:
    """Enumerate, order and integrate the outstanding published branches.

    `log(entry_type, data=...)` matches `TranscriptLogger.append` and
    `Orchestrator._log`, so the caller passes whichever it already holds and
    every sweep lands in the same transcript as everything else.

    `merger` exists for tests that want to observe the attempt ORDER without a
    real merge; production leaves it `None` and gets a real `AutoMerger` built
    from the same collaborators.
    """

    def __init__(
        self,
        *,
        config: AutoloopConfig,
        git: GitGateway,
        policy: PolicyEngine,
        execution_store: TaskExecutionStore,
        registry,
        log,
        merger=None,
        carry_forward=None,
    ):
        self._config = config
        self._git = git
        self._policy = policy
        self._execution_store = execution_store
        self._registry = registry
        self._log = log
        #: Read once per sweeper, and only when a candidate this checkout does
        #: not hold actually needs them. `()` is "not looked at yet" and
        #: `(value,)` is "looked at, and this is the answer" — a one-cell tuple
        #: rather than `None`, because `merge_commit_subjects` answers `None`
        #: for a walk it could not complete and that answer has to be memoized
        #: as readily as a successful one.
        self._commit_map_cache: tuple = ()
        self._merge_index_cache: tuple = ()
        self._merger = merger or AutoMerger(
            config=config,
            git=git,
            policy=policy,
            execution_store=execution_store,
            registry=registry,
            log=log,
            #: Passed straight through, never built here. See `AutoMerger`'s own
            #: field: at `lanes > 1` a merge that moves the head past a bound
            #: candidate has to carry that candidate forward, and the fetch
            #: source for it is the loop-owned observed clone, which only the
            #: orchestrator can resolve. `None` — the startup sweep, which has no
            #: orchestrator — makes such a merge DEFER, which is exactly what the
            #: shut window does at one lane and mutates nothing.
            carry_forward=carry_forward,
        )

    # ---- entry point --------------------------------------------------------

    def sweep(self) -> SweepResult:
        """Integrate every outstanding branch, oldest first, stopping at the
        first one that does not land. Never raises — see the module docstring.
        """
        if not self._policy.config.auto_merge_enabled:
            return SweepResult(outcome=DISABLED)
        #: Confirmed publications, memoized for this invocation only, exactly
        #: as `_cmd_merge_window` and `AutoMerger.after_completion` do it. It
        #: spares the OTHER records the gate re-reads on every branch a repeat
        #: round-trip. It is emphatically NOT authority for the branch about to
        #: be merged: `_reconfirm` evicts that key first, so every merge runs on
        #: an `ls-remote` taken after the previous merge, not before the sweep.
        seen: set = set()
        result = SweepResult(outcome=NOTHING_TO_DO)
        # The pre-SWEEP head, read before anything is enumerated. Every early
        # return below happens before a single merge, so `base_after` starts
        # equal to it and is only rewritten where the checkout could actually
        # have moved. A probe that cannot read leaves both `""` rather than
        # raising; `_backlog`'s own `head_sha()` is the read that decides
        # whether a git that will not answer is fatal.
        start = _probe(self._git)
        result.base_before = result.base_after = start.head
        try:
            candidates = self._backlog(seen, result)
        except Exception as exc:      # noqa: BLE001 - a sweep must not stop a run
            self._log("merge_sweep_error", data={"error": f"{type(exc).__name__}: {exc}"})
            # Whatever the enumeration managed to judge before it blew up is
            # carried out with it, exactly as the gate-failure path below does:
            # the run is not clear either way, but the tasks it could not judge
            # are the ones an operator has to go and look at. Nothing has been
            # merged at this point, so the checkout is not in question.
            return SweepResult(
                outcome=FAILED,
                unresolved=result.unresolved,
                base_before=start.head,
                base_after=start.head,
            )
        if not candidates:
            # ONE terminal entry, so a reader of this transcript can tell a
            # sweep that found nothing from a sweep that never ran. Per-BRANCH
            # silence is untouched and stays deliberate (a branch already in the
            # base is the ordinary case for every task the loop has completed);
            # this is one line per INVOCATION, and it is what lets
            # `health.held_merge_sweep` stop reporting a hold that has ended.
            # `unresolved` rides along because this path is reached with it
            # non-empty — an unjudgeable task with no branch queued behind it —
            # and that is a held backlog with an empty queue, not a clear run.
            self._log(
                SWEEP_IDLE_EVENT,
                data={
                    "unresolved": [task_id for task_id, _why in result.unresolved],
                    "pending": [],
                },
            )
            return result

        result.pending = [c.task_id for c in candidates]
        self._log(
            "merge_sweep_backlog",
            data={
                "pending": list(result.pending),
                "detail": [
                    {"task_id": c.task_id, "candidate_sha": c.candidate_sha,
                     "dest_ref": c.dest_ref}
                    for c in candidates
                ],
            },
        )

        # A task the enumeration could not JUDGE holds the whole invocation,
        # before the gate and before any merge. Not because the sweep is tidy
        # about reporting, but because an unjudgeable task is not provably
        # independent of the ones below it: this module deliberately supports a
        # later branch being cut from an earlier one, so merging a confirmed
        # candidate can carry an unconfirmed ancestor into the base with it.
        # See the module docstring's "could not look" section, including what
        # this deliberately costs.
        if result.unresolved:
            result.outcome = HELD
            self._log(
                SWEEP_HELD_EVENT,
                data={
                    "unresolved": [task_id for task_id, _why in result.unresolved],
                    "pending": list(result.pending),
                },
            )
            return result

        # THE GATE, once, for the whole sweep. See the module docstring.
        from . import cli

        try:
            reasons, notes = cli._merge_window_blockers(self._config, seen, self._git)
        except Exception as exc:      # noqa: BLE001 - fail closed, never merge
            self._log("merge_sweep_error", data={"error": f"{type(exc).__name__}: {exc}"})
            return SweepResult(
                outcome=FAILED,
                pending=result.pending,
                unresolved=result.unresolved,
                base_before=start.head,
                base_after=start.head,
            )
        for note in notes:
            self._log("merge_sweep_window_note", data={"note": note})
        if reasons:
            result.outcome = DEFERRED
            result.reasons = list(reasons)
            self._log(
                SWEEP_DEFERRED_EVENT,
                data={"reasons": list(reasons), "pending": list(result.pending)},
            )
            return result

        for index, candidate in enumerate(candidates):
            # Probed per BRANCH rather than once for the sweep, because the
            # branches that already landed moved HEAD legitimately: each was
            # verified and pushed before the next was attempted. The only head
            # move in question is the one the STOPPING attempt made, so the
            # baseline has to be where that attempt found the checkout.
            before_attempt = _probe(self._git)
            outcome = self._attempt(candidate, seen)
            if outcome not in _CONTINUE_ON:
                # Read from the checkout, never inferred from `outcome`. A
                # refused push returns `auto_merge.DEFERRED` — the slug that
                # otherwise means "a precondition failed, nothing was touched"
                # — over a base that has already moved locally. See the module
                # docstring's "a stop is not automatically a restoration".
                after_attempt = _probe(self._git)
                result.outcome = STOPPED
                result.stopped_on = candidate.task_id
                result.stopped_outcome = outcome
                result.pending = [c.task_id for c in candidates[index:]]
                result.base_after = after_attempt.head
                result.unreconciled = _unreconciled(before_attempt, after_attempt)
                self._log(
                    SWEEP_STOPPED_EVENT,
                    data={
                        "task_id": candidate.task_id,
                        "outcome": outcome,
                        "merged": list(result.merged),
                        "remaining": list(result.pending),
                        "base_sha_at_start": result.base_before,
                        "base_sha_before_attempt": before_attempt.head,
                        "base_sha_after_attempt": after_attempt.head,
                        "unreconciled": result.unreconciled,
                    },
                )
                return result
            result.merged.append(candidate.task_id)

        result.outcome = SWEPT
        result.pending = []
        # Every branch here returned `merged` (verified AND pushed) or
        # `already_integrated` (the remote base already carried it), so the head
        # moved only into states `AutoMerger` confirmed. Recorded for the report,
        # not questioned.
        result.base_after = _probe(self._git).head
        self._log(
            SWEEP_COMPLETED_EVENT,
            data={
                "merged": list(result.merged),
                "base_sha_before": result.base_before,
                "base_sha_after": result.base_after,
            },
        )
        return result

    def _attempt(self, candidate: SweepCandidate, seen: set) -> str:
        """One branch, through the shared merge machinery, on publication
        evidence taken NOW rather than at enumeration.

        An exception is an outcome like any other here — it stops the sweep,
        because a branch that blew up mid-merge is exactly the state nothing
        further should be stacked onto."""
        try:
            published, why_not = self._reconfirm(candidate, seen)
            if not published:
                self._log(
                    "merge_sweep_publication_changed",
                    data={
                        "task_id": candidate.task_id,
                        "candidate_sha": candidate.candidate_sha,
                        "dest_ref": candidate.dest_ref,
                        "reason": why_not,
                    },
                )
                return UNCONFIRMED
            return self._merger.attempt(candidate.task_id, seen)
        except Exception as exc:      # noqa: BLE001 - deliberate; see the docstring
            self._log(
                "merge_sweep_error",
                data={
                    "task_id": candidate.task_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return auto_merge.FAILED

    def _reconfirm(self, candidate: SweepCandidate, seen: set) -> tuple[bool, str]:
        """Does the remote STILL carry this candidate, asked right now?

        The eviction is the whole method. `seen` was populated during
        enumeration, potentially several merges and pushes ago; leaving this
        key in it would answer "published" from a cache instead of from the
        remote, which is the one question that must never be answered from a
        cache here (see the module docstring). Re-added on success by
        `_candidate_publication` itself, so `AutoMerger.attempt`'s own check
        does not pay for a second round-trip.
        """
        from . import cli

        seen.discard((candidate.remote, candidate.dest_ref, candidate.candidate_sha))
        return cli._candidate_publication(
            self._config,
            _publication_dict(
                candidate.candidate_sha, candidate.remote, candidate.dest_ref
            ),
            seen,
            self._git,
        )

    # ---- enumeration --------------------------------------------------------

    def _backlog(self, seen: set, result: SweepResult) -> list[SweepCandidate]:
        """Every completed task whose published candidate is not an ancestor of
        the base head, ordered oldest publication first.

        Ordered cheapest-first, like `cli._candidate_is_retired`: registry
        state, then a local file read, then local git, and only then the
        remote. A task that fails an earlier check never costs a round-trip.
        """
        from . import cli

        head = self._git.head_sha()
        candidates: list[SweepCandidate] = []
        for task in self._registry.all_tasks():
            task_id = task.id
            # COMPLETED and nothing else. That single test is also what keeps a
            # SHIPPED_ELSEWHERE record out of the sweep — it never had a branch,
            # so asking it for one would mint an `unresolved` entry that holds
            # every invocation. See the module docstring's "what is NOT
            # enumerated"; the rule for a genuinely completed task is untouched.
            if self._registry.state_of(task_id) is not TaskState.COMPLETED:
                continue
            try:
                record = self._execution_store.load(task_id)
            except (StateCorruptError, OSError) as exc:
                # Not fatal to the sweep — an unreadable record is one branch
                # this cannot see, not a reason to leave the other six unmerged
                # — but never silent either. Completion implies a confirmed
                # publication, so the record that will not load is the only
                # thing naming a branch that may well be outstanding. Until
                # 2026-08-15 this logged and then `continue`d, so a sweep whose
                # only completed task had a torn record exited 0.
                self._unresolved(
                    result, task_id, f"its execution record could not be read ({exc})"
                )
                continue
            if record is None:
                # Retired by `retire_execution` (`release`, or
                # `_reconcile_published_execution`, which completes the task and
                # archives the record in one call). Only the archive can say
                # whether that retirement happened over work that had already
                # landed; anything less is a guess.
                integrated, why_not = self._retired_publication_is_integrated(
                    head, task_id
                )
                if not integrated:
                    self._unresolved(result, task_id, why_not)
                continue
            if not record.candidate_sha:
                # A completed task's record HAS to name the candidate that was
                # published — that is what completion means here. One that does
                # not cannot be describing this task's publication, so the
                # branch behind it is unaccounted for rather than absent.
                #
                # Except that a RE-DISPATCH leaves exactly this: an empty stub
                # over a task whose earlier work was retired, and whose archived
                # FILENAME still names the sha it was retired over (dash-02,
                # pkt-02, pkt-03, audit-0001). There is no publication to check
                # here — the record names no branch at all — so both the
                # filename and the merge-subject rules are available at once.
                recovery = self._recover(head, task_id, "", merge_commit=True)
                if recovery.verdict in CANDIDATE_RESOLVED:
                    self._log_recovered(task_id, recovery)
                    continue
                self._unresolved(
                    result,
                    task_id,
                    "its execution record names no candidate, though completion "
                    f"means one was published — {recovery.detail}",
                )
                continue
            # ANCESTRY, and nothing else, decides merged-ness. Silent on
            # purpose: a branch already in the base is the ordinary case for
            # every task the loop has ever completed, and one log line each
            # would bury the handful that actually need integrating.
            if self._is_integrated(head, record.candidate_sha):
                continue
            # Is the commit the record names even IN this repository? The whole
            # pre-extraction section hangs off that question and nothing else:
            # False here is the 2026-08-27 rewrite (or a candidate this checkout
            # has never fetched, which the publication check below still handles
            # exactly as it always has), True is an ordinary outstanding branch
            # that must keep going down the path it has always taken.
            local = self._candidate_is_local(record.candidate_sha)
            if not local:
                recovery = self._recover(
                    head, task_id, record.candidate_sha, merge_commit=False
                )
                if recovery.verdict in CANDIDATE_RESOLVED:
                    self._log_recovered(task_id, recovery)
                    continue
                if recovery.verdict == CANDIDATE_OUTSTANDING:
                    # The map ANSWERED and the answer was "still outstanding".
                    # Named, never attempted: the record's own sha is not an
                    # object anything can merge from, and the ref that would
                    # carry the rewritten one is not what the record names.
                    self._unresolved(result, task_id, recovery.detail)
                    continue
            published, why_not = cli._candidate_publication(
                self._config, _as_record_dict(record), seen, self._git
            )
            if not published and not local:
                # Nothing on the remote carries this candidate AND this
                # repository does not hold it, which is the shape a discarded
                # pre-extraction candidate leaves (brw-18). Only now is the
                # merge-subject rule allowed to look — see `_recover`.
                recovery = self._recover(
                    head, task_id, record.candidate_sha, merge_commit=True
                )
                if recovery.verdict in CANDIDATE_RESOLVED:
                    self._log_recovered(task_id, recovery)
                    continue
                why_not = f"{why_not}; {recovery.detail}" if recovery.detail else why_not
            if not published:
                # Completed + unmerged + no branch on the remote carrying this
                # candidate. `_mark_task_completed` only fires on a confirmed
                # publication, so this means the ref was deleted or force-moved
                # afterwards — or the remote cannot be reached right now. Named
                # rather than merged: there is nothing here this module is
                # allowed to integrate, and inventing a branch to merge from a
                # record's own claim is the fail-open reading
                # `_candidate_publication` exists to refuse.
                self._unresolved(result, task_id, why_not)
                continue
            candidates.append(
                SweepCandidate(
                    task_id=task_id,
                    candidate_sha=record.candidate_sha,
                    remote=record.intended_remote,
                    dest_ref=record.intended_remote_ref,
                    order=self._publication_order(task_id, record),
                )
            )
        candidates.sort(key=lambda c: c.order)
        return candidates

    def _unresolved(self, result: SweepResult, task_id: str, why_not: str) -> None:
        """Record a completed task the sweep could not judge. ONE funnel, so
        every such state reaches the same list, the same transcript entry and
        therefore the same exit code — the four of them differ only in the
        reason string, and a second path that forgot one of those three is how
        the corrupt-record case came to exit 0."""
        result.unresolved.append((task_id, why_not))
        self._log("merge_sweep_unresolved", data={"task_id": task_id, "reason": why_not})

    def _retired_publication_is_integrated(
        self, head: str, task_id: str
    ) -> tuple[bool, str]:
        """A COMPLETED task with no live execution record: is its work provably
        in the base already? `(True, "")` only when it demonstrably is.

        The reading itself — which archived generation answers, whose copies are
        dropped, and what an unorderable or unreadable archive does — is
        `execution_record_ancestry`, shared with `dashboard.registry_
        disagreements` since witness-01 (2026-08-27). This method is the sweep's
        side of it and nothing else: the verdict collapsed to the `(bool, why)`
        `_backlog` records, with the ancestry question answered by
        `_is_integrated` and the reason phrased against `head`.

        Why the archive is read here at all, when `cli._merge_window_blockers`
        deliberately does NOT recurse into it: the two ask different questions.
        The gate asks "could moving the base strand this?", and a retired record
        describes work nobody is going to build on, so ignoring it is right.
        This asks "is this branch in the base?", which retirement does not
        answer either way — `retire_execution` files a record away on
        publication being CONFIRMED, not on it having been merged, and with
        `auto_merge_enabled` off nothing merges it at all. Skip the archive and
        every such branch becomes invisible again, which is the whole failure
        this module exists to end.

        The ancestry callable answers `"yes"`/`"no"` and never `"unknown"`,
        because `_is_integrated` already collapses a git failure to "not
        integrated here" — an object this checkout cannot resolve cannot be an
        ancestor of its own HEAD, and `attempt` fetches it before deciding
        anything. So the shared helper's `unverified` verdict is unreachable
        from this caller, and every other verdict maps to the same
        report-and-do-not-merge answer it always did.

        Nothing here is ever merged FROM: an archived record is not a merge
        source, because `AutoMerger.attempt` reads the LIVE record and would
        skip the task anyway. The honest report is the output.

        A `not-in-base` or `absent` verdict is then offered to `_recover_retired`
        before it is reported, because the 2026-08-27 rewrite renamed the commit
        an archived record names exactly as it renamed a live one. That recovery
        can only turn "unresolved" into "accounted for", never into a merge, and
        the wording of every verdict it does not answer is untouched.
        """
        answer = execution_record_ancestry(
            self._execution_store.directory,
            task_id,
            lambda sha: "yes" if self._is_integrated(head, sha) else "no",
            base_label=head[:12],
        )
        if answer.verdict == RECORD_IN_BASE:
            return True, ""
        recovery = self._recover_retired(head, task_id, answer)
        if recovery is not None:
            self._log_recovered(task_id, recovery)
            return True, ""
        return False, answer.detail

    def _recover_retired(
        self, head: str, task_id: str, answer: RecordAncestry
    ) -> CandidateRecovery | None:
        """The pre-extraction rules, applied to a task whose live record was
        RETIRED — `None` when they answer nothing, which leaves the verdict and
        the wording above exactly as they were.

        Reached for the same reason the live-record path is: the 2026-08-27
        rewrite renamed the commit an archived record names just as thoroughly as
        the one a live record names, and `execution_record_ancestry` asks git
        about a sha that is not in this object database. It is deliberately NOT
        inside that shared function — `dashboard.registry_disagreements` asks it
        the same question and must keep getting the same answer — so the recovery
        is the sweep's own, exactly like the ancestry callable it injects.

        The merge-subject rule IS allowed here, unlike on the live path. There is
        no publication to check and no branch to skip: a retired record is never
        a merge source (`AutoMerger.attempt` reads the LIVE record), so the only
        two endings are "accounted for" and "unresolved", and no confirmed branch
        can be lost by looking.

        MORE THAN ONE sha and this refuses outright. `_record_verdict` requires
        EVERY copy in a tied generation to be integrated, precisely because two
        retirements inside the same second cannot be ordered; taking any-one-of
        here would be a weaker rule wearing the same name.
        """
        if answer.verdict not in (RECORD_NOT_IN_BASE, RECORD_ABSENT):
            # `unverified` is "could not look" — an unreadable copy, an archive
            # that cannot be listed or ordered. Nothing here may turn that into
            # an answer.
            return None
        if len(answer.shas) > 1:
            return None
        recovery = self._recover(
            head,
            task_id,
            answer.shas[0] if answer.shas else "",
            merge_commit=True,
            named_by="its newest archived execution record",
        )
        return recovery if recovery.verdict in CANDIDATE_RESOLVED else None

    def _is_integrated(self, head: str, candidate_sha: str) -> bool:
        """Is `candidate_sha` already in the base? An object git cannot resolve
        answers False: a commit that is an ancestor of HEAD is in this object
        database by definition, so "cannot resolve" IS "not integrated here"."""
        try:
            return self._git.is_descendant(head, candidate_sha)
        except (GitError, OSError):
            return False

    # ---- a candidate the 2026-08-27 extraction rewrote ----------------------
    #
    # The rules themselves are module-level (see the section of that name);
    # these methods are the sweep's side of them — the ordering, the git
    # questions, and the one transcript entry they write.

    def _candidate_is_local(self, sha: str) -> bool:
        """Does THIS repository hold the object the record names?

        The gate on the whole section: a candidate git can resolve is judged
        exactly as it was before, so nothing here can reach the ordinary case of
        a branch this checkout has simply never fetched. `object_exists` raises
        rather than answering when git could not look (an abbreviated name it
        cannot resolve dies with 128, which is not "absent"), and that is
        collapsed to False deliberately: the only thing downstream does with
        False is consult evidence that names the commit some other way, and none
        of that evidence can merge anything.

        A gateway with no such probe answers False for the same reason and with
        the same consequence — the rules below are then reached and resolve
        whatever the map and the archive can prove, which for a gateway that
        cannot answer this is usually nothing at all.
        """
        probe = getattr(self._git, "object_exists", None)
        if probe is None:
            return False
        try:
            return probe(sha)
        except (GitError, OSError):
            return False

    def _commit_map(self) -> CommitMap:
        """The vendored map, read at most once per sweep. A gateway that will
        not name its repository root yields a map with an `error`, never one
        read from `Path.cwd()`: the sweep's checkout is the only place this file
        is evidence about."""
        if not self._commit_map_cache:
            root = getattr(self._git, "repo_root", None)
            self._commit_map_cache = (load_commit_map(root),)
        return self._commit_map_cache[0]

    def _merge_subject_index(self, head: str):
        """`merge_commit_subjects` over the base head, at most once per sweep.

        Memoized because the walk costs one `cat-file` per commit on the
        first-parent chain and every unrecovered task would otherwise repeat it.
        Reached only where the sweep is already reporting a task it cannot judge
        — a failed publication check, a record naming no candidate, a retired one
        — so the ordinary sweep, every candidate confirmed on the remote, never
        pays for it at all.
        """
        if not self._merge_index_cache:
            self._merge_index_cache = (merge_commit_subjects(self._git, head),)
        return self._merge_index_cache[0]

    def _recover(
        self,
        head: str,
        task_id: str,
        candidate_sha: str,
        *,
        merge_commit: bool,
        named_by: str = "its execution record",
    ) -> CandidateRecovery:
        """What the evidence OUTSIDE this checkout's object database says about
        a candidate the object database does not hold.

        The order is fixed and each step is tried once: the sha the record
        names, or the one named by the newest archived FILENAME that carries one
        when the record names none; then the commit map; then — only when
        `merge_commit` — a merge commit on the base head's first-parent chain.

        `merge_commit` is False on the first pass and True only after the
        publication check has failed, and that is not an optimisation. A task
        that was merged once and RE-dispatched carries `Merge task <id>` in the
        base while its NEW branch is genuinely outstanding and confirmed on the
        remote; reading the old merge as an answer about the new branch would
        skip a branch the sweep exists to merge.

        Every step that cannot answer appends its reason and the next is tried.
        Nothing is guessed at, and a run out of steps is `CANDIDATE_UNRECOVERED`
        — which leaves the task exactly where it was before this existed.
        """
        tried: list[str] = []
        sha = str(candidate_sha or "")
        rule = RULE_RECORD
        if not sha:
            sha, source, why_not = archived_candidate_sha(
                self._execution_store.directory, task_id
            )
            # Not "its NEWEST archived record": the scan passes over labels the
            # pattern does not apply to, so the copy that answered is the newest
            # one NAMING a sha and often not the newest one on disk (audit-0001).
            # `source` says which it was, and the operator greps this line.
            named_by, rule = f"its archived record ({source})", RULE_ARCHIVE
            if not sha:
                tried.append(why_not)
        if sha:
            answer = self._judge_recovered_sha(head, sha, named_by, rule, tried)
            if answer is not None:
                return answer
        if merge_commit:
            answer = self._judge_merge_subject(head, task_id, tried)
            if answer is not None:
                return answer
        return CandidateRecovery(
            CANDIDATE_UNRECOVERED, "; ".join(reason for reason in tried if reason)
        )

    def _judge_recovered_sha(
        self, head: str, sha: str, named_by: str, rule: str, tried: list
    ) -> CandidateRecovery | None:
        """One sha, judged: by ancestry when this repository holds it, and
        otherwise through the map. `None` means "no answer, try the next rule",
        with the reason appended to `tried`.

        **A row in the map is FINAL.** Mapped to a commit that is not an
        ancestor is `outstanding` — an honest "this branch may still need
        merging" — and is deliberately not passed on to the merge-subject rule:
        the map is the authoritative link between the two histories, and letting
        a commit subject overrule it would be exactly the written-claim
        clearance this section refuses.
        """
        if self._candidate_is_local(sha):
            if self._is_integrated(head, sha):
                return CandidateRecovery(
                    CANDIDATE_IN_BASE,
                    f"{named_by} names {sha[:12]}, which is an ancestor of "
                    f"{head[:12]} — the branch is accounted for in the base",
                    sha=sha,
                    rule=rule,
                )
            return CandidateRecovery(
                CANDIDATE_OUTSTANDING,
                f"{named_by} names {sha[:12]}, which this checkout holds and "
                f"which is not an ancestor of {head[:12]} — the branch may still "
                "be outstanding; merge it by hand",
                sha=sha,
                rule=rule,
            )
        commit_map = self._commit_map()
        if commit_map.error:
            tried.append(commit_map.error)
            return None
        new, why_not = commit_map.lookup(sha)
        if not new:
            tried.append(why_not)
            return None
        if new == NULL_SHA:
            return CandidateRecovery(
                CANDIDATE_PRUNED,
                f"{named_by} names {sha[:12]}, which {COMMIT_MAP_PATH} records as "
                "PRUNED by the 2026-08-27 extraction — it touched no path under "
                "autoloop/, so there is nothing in this repository to merge and "
                "nothing that should exist",
                sha=NULL_SHA,
                rule=RULE_COMMIT_MAP,
            )
        if self._is_integrated(head, new):
            return CandidateRecovery(
                CANDIDATE_IN_BASE,
                f"{named_by} names the pre-extraction commit {sha[:12]}, which "
                f"{COMMIT_MAP_PATH} rewrites to {new[:12]} — an ancestor of "
                f"{head[:12]}, so the branch is accounted for in the base",
                sha=new,
                rule=RULE_COMMIT_MAP,
            )
        return CandidateRecovery(
            CANDIDATE_OUTSTANDING,
            f"{named_by} names the pre-extraction commit {sha[:12]}, which "
            f"{COMMIT_MAP_PATH} rewrites to {new[:12]}"
            + (
                ""
                if self._candidate_is_local(new)
                else " — an object this checkout does not hold —"
            )
            + f" and that commit is not an ancestor of {head[:12]}; the work may "
            "still be outstanding and no ref here can be merged for it",
            sha=new,
            rule=RULE_COMMIT_MAP,
        )

    def _judge_merge_subject(
        self, head: str, task_id: str, tried: list
    ) -> CandidateRecovery | None:
        """The last resort: the loop's own merge commit for this task, on the
        first-parent chain of the base head.

        The id is matched by dict-key EQUALITY against the id parsed out of the
        subject, so `pkt-03` can never answer for `pkt-030` and a mention in a
        commit BODY is not looked at. Found means integrated: everything on that
        chain is an ancestor of the head by construction.
        """
        index = self._merge_subject_index(head)
        if index is None:
            tried.append(
                f"the first-parent chain of {head[:12]} could not be walked, so "
                "whether a merge commit names this task is unknown"
            )
            return None
        found = index.get(task_id)
        if found is None:
            tried.append(
                f"no merge commit on the first-parent chain of {head[:12]} has "
                f"the subject `Merge task {task_id} (<sha>)`"
            )
            return None
        merge_sha, named = found
        return CandidateRecovery(
            CANDIDATE_IN_BASE,
            f"the base head reaches merge commit {merge_sha[:12]}, whose subject "
            f"is `Merge task {task_id} ({named})` — the work was integrated under "
            "a candidate the execution record no longer names",
            sha=merge_sha,
            rule=RULE_MERGE_COMMIT,
        )

    def _log_recovered(self, task_id: str, recovery: CandidateRecovery) -> None:
        """One transcript line for a task the rules answered for. Deliberately
        not silent (see `SWEEP_RECOVERED_EVENT`): the records on disk are wrong,
        and the operator repairing them needs to see which rule answered."""
        self._log(
            SWEEP_RECOVERED_EVENT,
            data={
                "task_id": task_id,
                "verdict": recovery.verdict,
                "rule": recovery.rule,
                "sha": recovery.sha,
                "detail": recovery.detail,
            },
        )

    def _publication_order(self, task_id: str, record) -> tuple:
        """`(group, timestamp, task_id)`. See the module docstring."""
        stamp = _parse_iso(getattr(record, "published_at", ""))
        if stamp is not None:
            return (1, stamp, task_id)
        # No usable `published_at`: the record predates the field, so it is
        # older than anything that has one. The candidate's committer date
        # orders that older group among itself.
        return (0, self._commit_timestamp(record.candidate_sha), task_id)

    def _commit_timestamp(self, sha: str) -> float:
        """The candidate's committer timestamp, or 0.0 when the checkout
        cannot read it (an object it has never fetched, most often). Uses the
        `read_commit` the gateway already exposes — a sweep is not a reason to
        widen the git whitelist."""
        try:
            ident = self._git.read_commit(sha).get("committer", "")
        except (GitError, OSError):
            return 0.0
        return _ident_timestamp(ident)


# ---- is the checkout where we left it? ---------------------------------------


@dataclass(frozen=True)
class _Checkout:
    """Where the checkout is, read as ONE observation.

    Both halves matter and neither answers for the other. HEAD alone misses a
    conflict abort that returned 0 and left the tree conflicted (`_abort` has
    its own check for exactly that, and this is the sweep-level version of the
    same distrust); the working tree alone misses a merge that committed
    cleanly and then failed a verification check.

    `error` is not an absence but an ANSWER — "the checkout could not be read"
    — for the same reason `_ArchivedCopy` keeps its own: a probe that silently
    degraded to "looks unchanged" would let precisely the unreadable case
    through as safe.
    """

    head: str = ""
    #: `git status --porcelain` lines, so two observations compare literally
    #: rather than through a boolean that cannot tell one dirty tree from
    #: another.
    dirty: tuple = ()
    error: str = ""

    @property
    def readable(self) -> bool:
        return not self.error


def _probe(git) -> _Checkout:
    """One observation of the checkout. Never raises: this runs on the failure
    path of a module whose whole discipline is that an integration problem must
    not stop a run, and an unreadable checkout is a finding rather than a
    crash."""
    try:
        return _Checkout(head=git.head_sha(), dirty=tuple(git.dirty_files()))
    except Exception as exc:      # noqa: BLE001 - "could not look" is an answer
        return _Checkout(error=f"{type(exc).__name__}: {exc}")


def _probe_checkout(config: AutoloopConfig, git) -> _Checkout:
    """`_probe`, building the gateway when the caller has none — the startup
    guard's case, where the failure being covered may BE the construction.
    Gateway construction is inside the try for that reason: a gateway that will
    not build has to come back as an unreadable checkout, not as a second
    exception thrown from the handler for the first."""
    try:
        gateway = (
            git if git is not None
            else GitGateway(Path.cwd(), PolicyEngine(config.policy))
        )
    except Exception as exc:      # noqa: BLE001 - same reason as `_probe`
        return _Checkout(error=f"{type(exc).__name__}: {exc}")
    return _probe(gateway)


def _unreconciled(before: _Checkout, after: _Checkout) -> str:
    """`""` when the checkout is provably exactly where the attempt found it,
    otherwise why that cannot be claimed.

    Asked in this direction on purpose: the caller needs a REASON to print when
    the answer is bad, and a bare boolean would have to be paired with a second
    string that could drift from it. Unreadable is not "unchanged" — see the
    module docstring; it is the same "could not look" the enumeration refuses to
    round down, applied to the base instead of to a branch.
    """
    if not before.readable or not after.readable:
        return (
            "the checkout could not be read "
            f"({before.error or after.error}), so whether the attempt left HEAD "
            "or the working tree moved is unknown"
        )
    if before.head != after.head:
        return (
            f"HEAD moved from {before.head[:12]} to {after.head[:12]} during an "
            "attempt that did not complete — a merge is in this checkout that was "
            "not both verified and pushed"
        )
    if before.dirty != after.dirty:
        return (
            f"HEAD is still {after.head[:12]}, but the working tree is not what "
            f"the attempt found: before {_describe_tree(before.dirty)}, "
            f"after {_describe_tree(after.dirty)}"
        )
    return ""


def _describe_tree(dirty: tuple) -> str:
    """A `git status --porcelain` listing, short enough to print. Truncated
    rather than elided entirely: the first few entries are what tells an
    operator whether they are looking at conflict markers or at a stray file."""
    if not dirty:
        return "clean"
    shown = ", ".join(dirty[:4])
    return shown if len(dirty) <= 4 else f"{shown}, +{len(dirty) - 4} more"


# ---- helpers ----------------------------------------------------------------


def _publication_dict(candidate_sha: str, remote: str, dest_ref: str) -> dict:
    """`_candidate_publication` reads its input with `.get()`, so it takes a
    plain dict. Built by hand from the three fields it actually reads rather
    than `dataclasses.asdict`, which would copy the whole record (including
    the report text) on every enumeration — and, since `_reconfirm` asks the
    same question again per branch, on every merge as well."""
    return {
        "candidate_sha": candidate_sha,
        "intended_remote": remote,
        "intended_remote_ref": dest_ref,
    }


def _as_record_dict(record) -> dict:
    """The same three fields, off a `TaskExecution`."""
    return _publication_dict(
        record.candidate_sha, record.intended_remote, record.intended_remote_ref
    )


# ---- a candidate the 2026-08-27 extraction rewrote ---------------------------
#
# merge-09, 2026-08-31. See the module docstring's section of the same name for
# WHY these rules exist, what they may not do, and what was tried and reverted.
# This section is the rules themselves; `BacklogSweeper._recover` is where they
# are ordered and the only thing that calls them.

#: The vendored map, relative to the repository root. filter-repo's own
#: `.git/filter-repo/commit-map`, copied verbatim — `.git/` is never cloned, so
#: a worker repository can only reach it because it is TRACKED.
COMMIT_MAP_PATH = "docs/extraction/commit-map.tsv"

#: What filter-repo writes in the `new` column for a commit the rewrite PRUNED.
NULL_SHA = "0" * 40

#: The shortest abbreviation this module will look up in the map by prefix. Git's
#: own default abbreviation length, and the length the archived filenames carry
#: (`dash-02-reconciled-as-dd28dfa-<stamp>.json`). A shorter prefix is refused
#: rather than resolved: uniqueness is the real guard, but a two-character
#: prefix that happens to be unique in a small map is an accident, not evidence.
MIN_ABBREVIATED_SHA = 7

#: How far `merge_commit_subjects` will walk before giving up. Far past this
#: repository's first-parent chain; reaching it means the scan is INCOMPLETE,
#: which is reported as such and clears nothing. A bound at all because the walk
#: costs one `cat-file` per commit.
FIRST_PARENT_WALK_CAP = 2000

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX = re.compile(r"^[0-9a-f]+$")

#: `AutoMerger._merge`'s own commit message — `f"Merge task {task_id} "
#: f"({candidate[:12]}) into {base_branch}"` — read back. Anchored at the start
#: of the SUBJECT, so a task id in a commit BODY can never match, and the id is
#: a captured group compared by dict-key equality rather than by `in`, so
#: `pkt-03` never answers for `pkt-030`. The trailing `into <branch>` is not
#: required: the 91 such commits on this repository's mainline are not
#: guaranteed to have all been written by the same generation of that code, and
#: the sha in parentheses is already the discriminating part.
_MERGE_SUBJECT = re.compile(
    r"^Merge task ([A-Za-z0-9][A-Za-z0-9._-]{0,63}) \(([0-9a-f]{7,40})\)"
)

#: The two archive labels that name their own sha in the FILENAME, written by
#: the reconciler and by a hand-merge. `published-<sha>` is deliberately NOT
#: here: this rule is the narrow one the four re-dispatched stubs (dash-02,
#: pkt-02, pkt-03, audit-0001) need, and widening it to every label that happens
#: to end in hex is a different, larger claim.
_ARCHIVED_SHA_LABELS = ("reconciled-as", "merged-as")

#: What consulting the map, the archive filename and the merge subjects can
#: conclude about one completed task's candidate. `PRUNED` is emphatically not
#: `IN_BASE`: the commit touched no path under `autoloop/`, so there is nothing
#: here to merge AND nothing that should exist — reporting it as merged would
#: claim work landed that this repository never contained.
CANDIDATE_IN_BASE = "in-base"
CANDIDATE_PRUNED = "pruned"
CANDIDATE_OUTSTANDING = "outstanding"
CANDIDATE_UNRECOVERED = "unrecovered"

#: Every verdict `BacklogSweeper._recover` can return, pinned as a tuple so a
#: caller can assert it knows all of them rather than growing a silent `else`.
CANDIDATE_VERDICTS = (
    CANDIDATE_IN_BASE,
    CANDIDATE_PRUNED,
    CANDIDATE_OUTSTANDING,
    CANDIDATE_UNRECOVERED,
)

#: The two verdicts that mean "this task needs nothing from the sweep". Both are
#: SKIPS, never merges.
CANDIDATE_RESOLVED = (CANDIDATE_IN_BASE, CANDIDATE_PRUNED)

#: Which evidence answered, carried on the verdict so the transcript says it.
RULE_RECORD = "execution-record"
RULE_ARCHIVE = "archive-filename"
RULE_COMMIT_MAP = "commit-map"
RULE_MERGE_COMMIT = "merge-commit"


@dataclass(frozen=True)
class CandidateRecovery:
    """What the rules concluded about one task's candidate, and from what.

    `detail` is a sentence for an operator on EVERY verdict, including the two
    that resolve — a reader of the transcript has to be able to see WHICH rule
    cleared a task and on which sha, because the whole point of the exercise is
    that the records on disk are wrong and someone is going to repair them.

    `sha` is the commit that ANSWERED (the rewritten one for a map hit, the
    merge commit for a subject hit), never the old sha the record named — and
    forty zeros for a PRUNED verdict, because that is literally what the map
    answered and there is no commit to name.
    """

    verdict: str
    detail: str = ""
    sha: str = ""
    rule: str = ""


@dataclass(frozen=True)
class CommitMap:
    """filter-repo's old->new commit map, parsed once per sweep.

    `error` is an ANSWER — "there is no map to consult" — and not an absence,
    for the same reason `_ArchivedCopy` keeps its own: a map that silently
    degraded to "no rows, therefore nothing is pruned" would turn every
    pre-extraction candidate into an unanswerable one WITHOUT saying so, which
    is the fail-open this whole section is written against. A `CommitMap` with
    an `error` answers nothing at all; `lookup` is not even called on one.
    """

    rows: dict = field(default_factory=dict)
    error: str = ""

    def lookup(self, sha: str) -> tuple[str, str]:
        """`(new sha, "")` when the map answers, `("", why not)` when it does
        not. Exactly one of the two is ever non-empty.

        A full 40-hex key is looked up literally. A shorter one — which is what
        an archived filename carries — is matched by PREFIX and must hit exactly
        one row: two rows sharing a prefix is "I cannot tell", and this module
        does not guess. `MIN_ABBREVIATED_SHA` is the floor below which even a
        unique hit is refused.
        """
        key = str(sha or "").strip().lower()
        if _FULL_SHA.match(key):
            new = self.rows.get(key)
            if new is None:
                return "", f"{key[:12]} is not in {COMMIT_MAP_PATH}"
            return new, ""
        if not _HEX.match(key) or len(key) < MIN_ABBREVIATED_SHA:
            return "", (
                f"{key or '(empty)'} is not a sha {COMMIT_MAP_PATH} can be keyed "
                f"by (40 hex characters, or at least {MIN_ABBREVIATED_SHA} of them)"
            )
        matches = sorted(old for old in self.rows if old.startswith(key))
        if not matches:
            return "", f"{key} is not in {COMMIT_MAP_PATH}"
        if len(matches) > 1:
            return "", (
                f"{key} is a prefix of {len(matches)} rows in {COMMIT_MAP_PATH}, "
                "so which commit it names cannot be told"
            )
        return self.rows[matches[0]], ""


def parse_commit_map(text: str) -> dict[str, str]:
    """`{old sha: new sha}` out of filter-repo's map file.

    Both columns must be 40 hex characters. That is the whole validation, and it
    is enough: it drops the `old new` header with no special case, and drops a
    truncated or re-formatted line rather than reading half of it as a sha.

    A duplicated `old` key with CONFLICTING values is dropped entirely rather
    than resolved by position — the file is evidence, and a contradiction in it
    is not something to pick a side of. A duplicate that agrees with itself is
    kept, since there is nothing to disagree about.
    """
    rows: dict[str, str] = {}
    conflicting: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        old, new = parts[0].lower(), parts[1].lower()
        if not _FULL_SHA.match(old) or not _FULL_SHA.match(new):
            continue
        if old in rows and rows[old] != new:
            conflicting.add(old)
        rows.setdefault(old, new)
    for old in conflicting:
        rows.pop(old, None)
    return rows


def load_commit_map(repo_root) -> CommitMap:
    """Read and parse the vendored map, or come back saying why not.

    An unreadable, absent or empty map is a `CommitMap` carrying an `error` and
    no rows. Nothing downstream may treat that as "this commit is not in the
    map": absence of the map is absence of evidence, and every candidate it
    would have answered for stays unresolved.
    """
    if repo_root is None:
        return CommitMap(
            error="the checkout would not name its repository root, so "
            f"{COMMIT_MAP_PATH} could not be located"
        )
    path = Path(repo_root) / COMMIT_MAP_PATH
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CommitMap(error=f"{COMMIT_MAP_PATH} could not be read ({exc})")
    rows = parse_commit_map(text)
    if not rows:
        return CommitMap(
            error=f"{COMMIT_MAP_PATH} holds no usable old->new rows"
        )
    return CommitMap(rows=rows)


def archived_candidate_sha(executions_dir, task_id: str) -> tuple[str, str, str]:
    """`(sha, source filename(s), why not)` — the sha named in the FILENAME of
    the NEWEST archived record for `task_id` WHOSE LABEL CARRIES ONE.

    For the four tasks whose live execution record is an empty re-dispatched
    stub (dash-02, pkt-02, pkt-03, audit-0001) this is the only thing on disk
    that names the commit their completion was recorded over. The reconciler
    wrote it into the label — `<id>-reconciled-as-<sha>-<stamp>.json` — and the
    record INSIDE names nothing.

    SCANNED newest-first rather than read off the newest generation alone, which
    is where merge-09 left `audit-0001` (2026-08-31). Its archive holds
    `audit-0001-reconciled-as-07b659b-<stamp>.json` and, retired LATER, a
    `-report-recovered-by-operator-<stamp>` copy that names no sha at all.
    Inspecting only the newest found nothing and gave up. A label this rule's
    pattern does not apply to is not evidence that no archived label names the
    commit — it is a file the question was not asked of — so the scan passes
    over it and asks the next generation down. Among labels that DO carry a sha
    the newest still wins, and the scan stops at the first one it finds:
    stopping only once a sha RESOLVES would let a superseded reconciliation
    clear a task whose latest one is genuinely outstanding.

    The generation rules are not reimplemented here: `_read_record`,
    `_is_another_tasks_copy` and `_archived_generations` are the same three the
    archive has always been read through, and they are the part nobody
    reconstructs correctly twice. Only the question is new, so only the reading
    of the FILENAME is new.

    Fail-closed three times over. The label must be one of
    `_ARCHIVED_SHA_LABELS` and must follow this task's own id, so a sibling
    whose id shares a prefix cannot answer. An archive that cannot be put in
    generation order answers nothing at all — skipping the copy that carries no
    stamp and ordering the rest would be exactly the fail-open the ordering rule
    exists to refuse. And when a generation is a TIE — two retirements inside
    the same second — every copy in it must name the same sha, because there is
    no way to tell which came last; a tie in which one copy names a sha and
    another names none is refused for the same reason rather than passed over.
    """
    archive = Path(executions_dir) / "archive"
    try:
        paths = sorted(archive.glob(f"{task_id}-*.json"))
    except OSError as exc:
        return "", "", f"its execution archive could not be listed ({exc})"
    copies = [
        copy
        for copy in (_read_record(path) for path in paths)
        if not _is_another_tasks_copy(copy, task_id)
    ]
    if not copies:
        return "", "", "nothing in its execution archive names a candidate either"
    generations, why_not = _archived_generations(copies)
    if not generations:
        return "", ", ".join(c.name for c in copies), why_not
    passed_over: list[str] = []
    for generation in generations:
        source = ", ".join(c.name for c in generation)
        shas = [_filename_sha(copy.path, task_id) for copy in generation]
        if not any(shas):
            # The pattern does not apply to this retirement's label at all.
            # Recorded so the reason can name every copy that was looked at, and
            # passed over so an older label that DOES name a sha still answers.
            passed_over.append(source)
            continue
        if not all(shas) or len(set(shas)) != 1:
            # Only a TIE reaches this: one copy alone either names a sha or was
            # passed over above. Two retirements inside one second cannot be
            # ordered, so a disagreement between them — two shas, or one sha and
            # one label the pattern does not cover — is the question, not an
            # answer with a distraction beside it.
            return "", source, (
                f"its archived records ({source}) were retired in the same "
                "second and their filenames do not agree on one sha, so which "
                "of them describes the retirement cannot be told"
            )
        return shas[0], source, ""
    source = ", ".join(passed_over)
    if len(passed_over) == 1:
        return "", source, (
            f"its newest archived record ({source}) names no sha in its filename"
        )
    return "", source, (
        f"none of its archived records ({source}) names a sha in its filename"
    )


def _filename_sha(path: Path, task_id: str) -> str:
    """The sha an archive label names, or `""`.

    Anchored on the task id AND on a whole label from `_ARCHIVED_SHA_LABELS`, so
    `rt-1-b-reconciled-as-<sha>-<stamp>.json` cannot answer for `rt-1` — the
    same prefix hazard `_is_another_tasks_copy` documents, closed here by
    construction rather than by reading the file.
    """
    labels = "|".join(re.escape(label) for label in _ARCHIVED_SHA_LABELS)
    match = re.match(
        rf"^{re.escape(task_id)}-(?:{labels})-([0-9a-f]{{7,40}})(?:-|$)", path.stem
    )
    return match.group(1) if match else ""


def merge_commit_subjects(git, head: str, cap: int | None = None):
    """`{task_id: (merge sha, sha it names)}` for every `Merge task <id> (<sha>)`
    MERGE commit on the first-parent chain of `head` — or `None` when the walk
    could not be completed.

    FIRST-PARENT, and that is the whole reason this is cheap and sound at once.
    `AutoMerger._merge` runs `git merge --no-ff` while the base branch is checked
    out, so every merge it has ever made is on that chain and its first parent is
    the base as it was. Walking it therefore finds the loop's own integrations
    and nothing else — and every commit found is an ancestor of `head` BY
    CONSTRUCTION, so no second ancestry question is asked and no second failure
    mode exists.

    `None`, never `{}`, when git would not answer or the cap is reached. An
    incomplete walk that returned its partial index would report "no merge
    commit names this task" for a task whose merge is just past where it
    stopped, which is a false negative dressed as a search — the same collapse
    `dashboard.commit_subjects` refuses. `None` clears nothing.

    `AttributeError` is caught beside the git failures for that same reason and
    no other: a gateway that does not implement `read_commit` at all cannot be
    walked, which is one more way to have looked at nothing. It resolves nothing
    either way, so the only thing the distinction could change is whether a
    sweep crashes.

    A commit is only read as an integration when it has TWO OR MORE PARENTS. An
    ordinary commit whose subject happens to be shaped like a merge's is not one,
    and the loop's own merges always are.
    """
    # Read at CALL time, not bound as a default: the cap is a module constant a
    # test lowers to reproduce a truncated walk, and a default argument would
    # have captured its value at import.
    limit = FIRST_PARENT_WALK_CAP if cap is None else cap
    index: dict[str, tuple[str, str]] = {}
    sha = str(head or "")
    steps = 0
    while sha:
        if steps >= limit:
            return None
        try:
            info = git.read_commit(sha)
        except (GitError, OSError, AttributeError):
            return None
        parents = list(info.get("parents") or [])
        lines = str(info.get("message") or "").splitlines()
        if len(parents) >= 2:
            match = _MERGE_SUBJECT.match(lines[0] if lines else "")
            if match:
                # `setdefault`: the walk is newest-first, and a task merged more
                # than once keeps its newest merge. Any of them being on this
                # chain answers the question equally, so this is a reporting
                # choice rather than a decision.
                index.setdefault(match.group(1), (sha, match.group(2)))
        sha = parents[0] if parents else ""
        steps += 1
    return index


# ---- one task's execution record, asked about ancestry -----------------------
#
# witness-01, 2026-08-27. ONE implementation, TWO callers. It began as
# `_retired_publication_is_integrated`'s body — the sweep's rule for judging a
# task whose live record has been retired — and moved here when the second
# caller arrived: `dashboard.registry_disagreements`, which asks exactly this
# question of exactly these records before deciding that a completed task no
# commit subject names is unaccounted for.
#
# MEASURED 2026-08-25. `shipped-report` returned seven `completed_unwitnessed`
# rows and two of them (dash-02, scope-02) had shipped — their commit subjects
# simply never named them, while the execution record on disk named a candidate
# that git will confirm is in the base. The evidence was already here and
# already trusted by the sweep; the report reached a weaker conclusion from data
# it already had.
#
# A SECOND COPY OF THE GENERATION RULES WOULD BE WORSE THAN THAT BUG. They are
# the part nobody reconstructs correctly twice: newest generation only, ordered
# by the filename stamp, another task's copies dropped by the id they carry, an
# unorderable archive refused rather than guessed at. So the rules live here and
# both callers call them; neither is allowed a variant.

#: What consulting one task's execution record can conclude about its candidate.
#: The last two are as deliberately distinct as `dashboard.SHIPPED_STATES`' last
#: two: `RECORD_UNVERIFIED` means a record exists and could not be judged,
#: `RECORD_ABSENT` means nothing on disk names a candidate at all. Collapsing
#: them turns "I could not look" into "there is nothing to see", which is the
#: fail-open every reader of these records is written against.
RECORD_IN_BASE = "in-base"
RECORD_NOT_IN_BASE = "not-in-base"
RECORD_UNVERIFIED = "unverified"
RECORD_ABSENT = "absent"

#: Every verdict `execution_record_ancestry` can return. Pinned as a tuple so a
#: caller can assert it knows all of them rather than growing a silent `else`.
RECORD_VERDICTS = (
    RECORD_IN_BASE,
    RECORD_NOT_IN_BASE,
    RECORD_UNVERIFIED,
    RECORD_ABSENT,
)


@dataclass(frozen=True)
class RecordAncestry:
    """Where one task's execution record says its candidate is, and how sure.

    **WHAT `in-base` PROVES, AND WHAT IT DOES NOT.** It proves the BRANCH is
    accounted for in the base — the commit the record names is reachable from
    the base head. It does NOT prove this task's content is present. On
    2026-08-25 bind-01, dash-17 and split-01 were each recorded as superseded
    with `git merge -s ours`, which makes the candidate a genuine ancestor while
    taking none of its content: for bind-01 and dash-17 the work really is in
    the base (under bind-02's and dash-19's commits), and for split-01 it was
    discarded and split-04 redoes it. Ancestry cannot tell those apart and does
    not claim to.

    That is still strictly better than matching commit subjects, which gets the
    same three wrong for a worse reason — the `-s ours` subjects DO name the
    task ids, so a subject-only reading calls them shipped on the strength of a
    commit whose own body says it took no content. Read a verdict here as "is
    this branch accounted for in the base", never as "is this task's code
    present".

    `detail` is a sentence for an operator, carried on EVERY verdict including
    `in-base` — a reader that only phrases its bad news cannot say why a row
    left a list. `source` names the file(s) the answer came from (empty when no
    file was reached at all), and `shas` are the candidate/published shas the
    record named.
    """

    verdict: str
    detail: str = ""
    source: str = ""
    shas: tuple[str, ...] = ()


def execution_record_ancestry(
    executions_dir,
    task_id: str,
    ancestry,
    *,
    base_label: str = "the base head",
) -> RecordAncestry:
    """Is the candidate `task_id`'s execution record names in the base?

    The record is the LIVE one when there is one, and otherwise the NEWEST
    archived generation. `ancestry(sha) -> "yes" | "no" | "unknown"` is injected
    — `dashboard.is_ancestor` on one side, `BacklogSweeper._is_integrated` on
    the other — so this function reads files and decides, and asks git nothing
    itself. It never raises: every unreadable, unlistable and unorderable state
    comes back as a verdict with a reason.

    Records are read as raw JSON rather than through `TaskExecutionStore.load`:
    an archived record can predate any field that dataclass now requires, and
    `TaskExecution(**data)` would raise on it. The live record's path comes from
    the store itself (`path_for`), so the on-disk layout is spelled once.

    **ANCESTRY decides, and either sha the record names answers.**
    `published_sha` is CORROBORATION — the one field meaning "the remote
    confirmed this", written by `_dispatch_task_push` from an `ls-remote` — but
    requiring it would be a stricter rule with a date on it: the field only
    exists from 2026-08-15, so every record written before then would be
    permanently unjudgeable with no action an operator could take, even with its
    candidate demonstrably merged. Git is authoritative about what is in the
    base and needs no second opinion.

    **Only the NEWEST retirement answers, and it answers alone.**
    `TaskExecutionStore.archive` preserves one file per retirement and refuses
    to clobber an earlier one, so a single task legitimately has several
    generations on disk — a `release`, a later retry, the `published-<sha>`
    retirement that produced the state being judged. Those generations describe
    DIFFERENT commits. An earlier one is very often integrated precisely because
    it was superseded and its work reached the base another way, so "any
    archived copy names an ancestor" clears a task whose newest publication is
    still outstanding. Superseded generations are not required to be integrated,
    and must not be: a released attempt's candidate is usually abandoned, so
    demanding it would report every retried task unresolved forever — the same
    wolf-crying from the opposite direction.

    Generation comes from the archive FILENAME, not from the record inside it:
    `retire_execution` appends a fixed-width UTC instant to every label
    (`<task_id>-<reason>-<stamp>.json`, stamp `YYYYMMDDTHHMMSSZ`), and while
    whole labels do not order across differing reasons, that trailing component
    does. When it cannot be read — an unstamped label, a hand-made file — the
    generations cannot be ordered, and an unorderable archive is `unverified`
    rather than guessed at. A task with exactly ONE archived record needs no
    ordering and is judged directly, which is what keeps records written before
    the stamp existed answerable.

    **The glob matches by filename PREFIX, so it can land on another task's
    record.** Task ids may contain `-` (`TaskRegistry`'s rule admits it) and so
    may labels, so `rt-1-*.json` matches `rt-1-b-published-<stamp>.json`. A
    sibling's copy carrying the newest stamp would BECOME the newest generation
    and answer for a task it says nothing about, so each copy is checked against
    the `task_id` it carries and dropped only when it PROVES it belongs to
    someone else — see `_is_another_tasks_copy` for why "cannot tell" is kept.

    A record that names NO candidate at all is `absent`, not `not-in-base`:
    there is no claim to refute. That is the same answer as "no file anywhere",
    and both mean the base cannot be asked about this task through its record.

    See `RecordAncestry` for the limit on what an `in-base` verdict proves — it
    is about the branch being accounted for, never about its content surviving.
    """
    if not task_id or "/" in task_id or "\\" in task_id or task_id.startswith("."):
        # Not an id `TaskRegistry` could ever have issued
        # (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`), so no record was ever written
        # under it — and both reads below build a PATH out of this string, so a
        # separator or a leading dot would send the glob somewhere else
        # entirely. The sweep only ever passes registry ids; the page reads the
        # task file tolerantly and can hand over whatever is in it.
        return RecordAncestry(
            RECORD_ABSENT,
            f"no execution record could be read: {task_id!r} is not a task id "
            "this store can name",
        )
    store = TaskExecutionStore(executions_dir)
    live = store.path_for(task_id)
    if live.is_file():
        copy = _read_record(live)
        if copy.data is None:
            # Fail-CLOSED, exactly like the sweep's own live-record branch: a
            # record that will not load names a candidate nobody can read, and
            # calling that "nothing names a candidate" is the shape that made a
            # torn record exit 0 on 2026-08-15.
            return RecordAncestry(
                RECORD_UNVERIFIED,
                f"its live execution record could not be read ({copy.error})",
                source=copy.name,
            )
        return _record_verdict([copy], ancestry, base_label, retired=False)
    archive = Path(executions_dir) / "archive"
    try:
        paths = sorted(archive.glob(f"{task_id}-*.json"))
    except OSError as exc:
        return RecordAncestry(
            RECORD_UNVERIFIED, f"its execution archive could not be listed ({exc})"
        )
    copies = [
        copy
        for copy in (_read_record(path) for path in paths)
        if not _is_another_tasks_copy(copy, task_id)
    ]
    if not copies:
        # Either the glob found nothing, or everything it found proved to
        # belong to another task — which is the same answer: nothing here
        # names the branch this task's completion says was published.
        return RecordAncestry(
            RECORD_ABSENT,
            "it has no execution record, live or archived — nothing names "
            "the candidate its completion says was published",
        )
    newest, why_not = _newest_generation(copies)
    if not newest:
        return RecordAncestry(
            RECORD_UNVERIFIED, why_not, source=", ".join(c.name for c in copies)
        )
    unreadable = [f"{c.name} ({c.error})" for c in newest if c.data is None]
    if unreadable:
        return RecordAncestry(
            RECORD_UNVERIFIED,
            "its record was retired and the newest archived copy could not "
            "be read: " + "; ".join(unreadable),
            source=", ".join(c.name for c in newest),
        )
    return _record_verdict(newest, ancestry, base_label, retired=True)


def _record_verdict(
    copies: list, ancestry, base_label: str, *, retired: bool
) -> RecordAncestry:
    """The ancestry verdict over the copies that get to answer — one live
    record, or one archived generation (which is several files only when two
    retirements share a second).

    Aggregation is per COPY and then ALL, which is what makes a tie
    conservative: with no way to tell which of two same-second retirements came
    last, both must be integrated before the task is clear. Inside one copy the
    two shas are ANY, because `candidate_sha` and `published_sha` describe the
    same publication and either one being in the base settles it.

    A copy naming no sha counts as not-integrated in a mixed group, and a group
    naming none at all is `absent` — see `execution_record_ancestry`.
    """
    source = ", ".join(c.name for c in copies)
    named: list[str] = []
    per_copy: list[str] = []
    for copy in copies:
        shas = [
            str(sha)
            for sha in (copy.data.get("candidate_sha"), copy.data.get("published_sha"))
            if sha
        ]
        named.extend(sha for sha in shas if sha not in named)
        verdicts = [ancestry(sha) for sha in shas]
        if "yes" in verdicts:
            per_copy.append("yes")
        elif "unknown" in verdicts:
            # Never rounded either way: an indeterminate check may not become a
            # clearance, and may not become a disagreement either.
            per_copy.append("unknown")
        else:
            per_copy.append("no")
    where = (
        f"its newest archived execution record ({source})"
        if retired
        else "its live execution record"
    )
    if not named:
        return RecordAncestry(
            RECORD_ABSENT,
            f"{where} names no candidate, though completion means one was "
            "published — nothing here names a branch to look for",
            source=source,
        )
    if all(verdict == "yes" for verdict in per_copy):
        return RecordAncestry(
            RECORD_IN_BASE,
            f"{where} names {named[0][:12]}, which is an ancestor of "
            f"{base_label} — the branch is accounted for in the base",
            source=source,
            shas=tuple(named),
        )
    if "unknown" in per_copy:
        return RecordAncestry(
            RECORD_UNVERIFIED,
            f"{where} names {len(named)} sha(s) and git could not resolve them "
            f"against {base_label} — no evidence either way",
            source=source,
            shas=tuple(named),
        )
    if retired:
        # The sweep's own wording, unchanged: `test_merge_sweep.py` reads these
        # reasons back, and an operator greps them.
        detail = (
            "its record was retired and no sha its newest archived copy "
            f"({source}) names is an ancestor of {base_label} — the branch may "
            "still be outstanding; merge it by hand"
        )
    else:
        detail = (
            f"its live execution record names {named[0][:12]} and no sha it "
            f"names is an ancestor of {base_label} — the branch may still be "
            "outstanding; merge it by hand"
        )
    return RecordAncestry(
        RECORD_NOT_IN_BASE, detail, source=source, shas=tuple(named)
    )


@dataclass(frozen=True)
class _ArchivedCopy:
    """One execution record on disk, read exactly once — a file in
    `executions/archive/`, or (since witness-01) the live record beside it.

    Read up front rather than on demand because the owner check and the
    ancestry check both need the contents, and a file that will not load has to
    survive as an ANSWER ("could not read this") rather than as an absence —
    dropping it silently is the shape that made the corrupt live record exit 0.
    """

    path: Path
    #: The record, or None when the file could not be read as one.
    data: dict | None = None
    #: Why, when `data` is None.
    error: str = ""

    @property
    def name(self) -> str:
        return self.path.name


def _read_record(path: Path) -> _ArchivedCopy:
    """Load one execution record as raw JSON. Never raises: an unreadable copy
    is a fact about the archive, and the caller reports it rather than crashing
    a sweep that runs before the loop starts."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _ArchivedCopy(path, None, str(exc))
    if not isinstance(data, dict):
        return _ArchivedCopy(path, None, "not a record")
    return _ArchivedCopy(path, data)


def _is_another_tasks_copy(copy: _ArchivedCopy, task_id: str) -> bool:
    """Does this copy PROVE it belongs to a different task?

    Only a readable record naming a different owner counts. The archive glob
    matches by filename prefix and both task ids and labels may contain `-`, so
    `rt-1-*.json` picks up `rt-1-b-published-<stamp>.json`; without this the
    sibling's copy could take the newest-generation slot and answer for a task
    it says nothing about. Asked in the negative on purpose — an unreadable copy
    and one with no `task_id` at all are both KEPT, because "cannot tell" must
    not read as "not mine": keeping them can only make the answer more
    conservative, while dropping them could clear an outstanding branch.
    """
    if copy.data is None:
        return False
    owner = copy.data.get("task_id")
    return isinstance(owner, str) and bool(owner) and owner != task_id


def _newest_generation(copies: list[_ArchivedCopy]) -> tuple[list[_ArchivedCopy], str]:
    """The archived record(s) describing the LATEST retirement of one task, or
    `([], why_not)` when the generations cannot be ordered.

    The ordering itself is `_archived_generations`; this is its first element,
    which is the only one the ancestry question wants. `archived_candidate_sha`
    wants the rest of the list as well, so the rules live there and neither
    caller is allowed a variant of them.
    """
    generations, why_not = _archived_generations(copies)
    return (generations[0] if generations else []), why_not


def _archived_generations(
    copies: list[_ArchivedCopy],
) -> tuple[list[list[_ArchivedCopy]], str]:
    """Every retirement of one task, NEWEST GENERATION FIRST — or `([], why_not)`
    when the generations cannot be ordered.

    One archived file is one retirement; several are several attempts at the
    same task, describing different commits (see
    `execution_record_ancestry`). Ordering them is therefore the whole
    question, and it is answered from the filename rather than the contents —
    the record inside carries no field that is written per-retirement.

    A single copy is returned untouched: one generation has nothing to be
    ordered against, so a record retired before the stamp existed is still
    answerable. From two upwards every label must carry a stamp, because an
    unstamped one could be the newest and there is no way to tell — the
    fail-closed answer, matching every other unanswerable question here. ALL of
    them are refused together: dropping the unstamped copy and ordering what is
    left would answer from a generation that may not be the newest, which is the
    one reading this refusal exists to prevent.

    Ties (two retirements inside the same second — `utcnow_iso` writes seconds)
    come back as ONE generation holding both, and a caller then requires all of
    them to agree. Same reasoning: with no way to tell which came last, the safe
    reading is the one that cannot clear an outstanding branch.

    The stamps sort as strings because they are fixed-width ASCII with the
    separators stripped (`_retirement_stamp`), so lexicographic order IS
    chronological order.
    """
    if len(copies) == 1:
        return [list(copies)], ""
    generations: dict[str, list[_ArchivedCopy]] = {}
    unstamped: list[str] = []
    for copy in copies:
        stamp = _retirement_stamp(copy.path)
        if not stamp:
            unstamped.append(copy.name)
            continue
        generations.setdefault(stamp, []).append(copy)
    if unstamped:
        return [], (
            "its record was retired more than once and "
            + ", ".join(sorted(unstamped))
            + " carries no retirement stamp, so the archived copies cannot be "
            "put in order — the newest may be describing work that is still "
            "outstanding; merge it by hand"
        )
    return [generations[stamp] for stamp in sorted(generations, reverse=True)], ""


def _retirement_stamp(path: Path) -> str:
    """The `YYYYMMDDTHHMMSSZ` instant `retire_execution` appends to every
    archive label, or `""` when this filename does not carry one.

    Read off the END of the stem rather than by stripping the task id from the
    front: `label = f"{reason}-{stamp}"` and both task ids and reasons contain
    `-` (`published-8d96c52aeca4`, `released-by-operator`), so the last
    `-`-separated component is the only one whose position is fixed. The stamp
    itself has none — `utcnow_iso().replace(":", "").replace("-", "")` strips
    them — which is what makes that split unambiguous and the result
    lexicographically ordered.

    Shape-checked rather than parsed: the value is only ever compared with
    another of its own kind, so a real instant is not needed, only a fixed-width
    ASCII one. `isascii()` is not decoration — `str.isdigit()` is true for
    non-ASCII digits, which sort nowhere useful.
    """
    tail = path.stem.rsplit("-", 1)[-1]
    if (
        len(tail) == 16
        and tail[8] == "T"
        and tail.endswith("Z")
        and tail[:8].isascii() and tail[:8].isdigit()
        and tail[9:15].isascii() and tail[9:15].isdigit()
    ):
        return tail
    return ""


def _parse_iso(value: str) -> float | None:
    """Epoch seconds for an ISO-8601 stamp, or None when there isn't one to
    read. A naive stamp is read as UTC — `state.utcnow_iso` only ever writes
    aware ones, and guessing the local zone for a legacy value would make the
    order depend on where the loop happens to run."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _ident_timestamp(ident: str) -> float:
    """The unix seconds out of a git ident (`Name <email> <unix-ts> <tz>`).
    0.0 for anything that does not parse — an unorderable record sorts first,
    which for this group is also the fail-safe direction (see the module
    docstring: a wrong order costs a stalled sweep, never a bad base)."""
    parts = ident.split()
    if len(parts) < 2:
        return 0.0
    try:
        return float(int(parts[-2]))
    except ValueError:
        return 0.0


# ---- construction -----------------------------------------------------------


def sweep_backlog(
    config: AutoloopConfig, *, git=None, log=None, carry_forward=None
) -> SweepResult:
    """Build the collaborators and sweep. The single entry point for both
    callers — `run`'s startup hook and the `merge-backlog` command — so the
    two cannot drift into sweeping different things.

    `GitGateway(Path.cwd(), ...)` matches every other gateway construction in
    `cli.py`: the operator runs the command from the checkout, and the loop
    process runs there too.

    `carry_forward` is `None` for both of today's callers, and deliberately: a
    startup hook and an operator command have no orchestrator, so neither can
    resolve the observed clone a carried-forward worker must fetch from. At
    `lanes > 1` that makes a merge with obligations DEFER rather than strand
    them — see `AutoMerger`'s own field. It is a parameter rather than a
    hard-coded `None` so a caller that HAS one (a lane's own round) does not
    have to rebuild the sweeper by hand to pass it.
    """
    from . import cli

    policy = PolicyEngine(config.policy)
    gateway = git if git is not None else GitGateway(Path.cwd(), policy)
    logger = log if log is not None else TranscriptLogger(config.transcript_file).append
    _, registry = cli._load_tasks(config)
    return BacklogSweeper(
        config=config,
        git=gateway,
        policy=policy,
        execution_store=TaskExecutionStore(config.executions_dir),
        registry=registry,
        log=logger,
        carry_forward=carry_forward,
    ).sweep()


def sweep_on_startup(config: AutoloopConfig, *, git=None, log=None) -> SweepResult:
    """`sweep_backlog` with the outer guard the startup path needs: an
    integration problem — a corrupt registry, an unreadable config, a git that
    will not answer — must not stop a run from starting. The sweep's own
    internals already swallow; this covers the CONSTRUCTION, the same way
    `orchestrator._auto_merge_after_completion` wraps `AutoMerger`'s.

    The one thing that DOES stop a run is a checkout this left somewhere it did
    not finish putting it, and a crash is the case where that is least visible:
    `sweep()` returns a result for every path it decides, so an exception
    reaching here came from construction (before any mutation) or from the
    transcript write that follows a stop (after one), and the slug alone cannot
    tell those apart. So it is not asked to. HEAD and the working tree are
    observed before and after, and the answer is a comparison — which keeps the
    ordinary crash reporting-and-continuing, exactly as documented, while a
    crash that really did leave the base moved is caught by
    `cli._sweep_backlog_on_startup` instead of being dispatched onto.

    The flag check stays FIRST, ahead of the probe: with `auto_merge_enabled`
    off (the default) nothing here may touch git at all, including to look.
    """
    if not config.policy.auto_merge_enabled:
        return SweepResult(outcome=DISABLED)
    before = _probe_checkout(config, git)
    try:
        return sweep_backlog(config, git=git, log=log)
    except Exception as exc:      # noqa: BLE001 - a sweep must not stop a run
        try:
            entry = log or TranscriptLogger(config.transcript_file).append
            entry("merge_sweep_error", data={"error": f"{type(exc).__name__}: {exc}"})
        except Exception:         # noqa: BLE001 - logging the failure may fail too
            pass
        after = _probe_checkout(config, git)
        return SweepResult(
            outcome=FAILED,
            base_before=before.head,
            base_after=after.head,
            unreconciled=_unreconciled(before, after),
        )
