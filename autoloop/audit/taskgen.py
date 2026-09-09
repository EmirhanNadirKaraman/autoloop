"""Findings → proposed task graph.

The output is a PROPOSAL: it is embedded in the audit report for ChatGPT,
which adopts it (possibly edited) via a normal contract-v3 `plan` decision —
the registry is never mutated by the audit itself.

**PLANNING DISCIPLINE (ctx-06).** A proposed task may not assert a repository
fact nobody read, and generation may not choose between sources that disagree.
Both are enforced here, on `inbox`'s existing primitives — `Evidence`, `Claim`,
`unsupported_claims`, `detect_conflicts` — rather than by a second evidence
system beside them:

  * every REPOSITORY CLAIM a finding makes (`Finding.claims`) must carry a
    citation naming a real reader or a stated assumption. One that carries
    neither is REFUSED, the finding becomes no task, and the refusal names the
    claim — in `TaskGraphProposal.skipped`, which `audit/report.py` already
    renders for the operator.
  * a disagreement between two sources about one subject is RECORDED durably
    through `blockers.BlockerStore` (a park record, not a field in
    `state.json`, so it survives a set-aside and a reset and can be answered
    later). A conflict that changes SCOPE stops generation; one that provably
    does not is recorded and generation continues.

WHICH TIERS ARE ACTUALLY FED, said plainly, because "represented" and "wired"
are different and only one of them is a guarantee:

  * ACCEPTED TASKS AND DECISIONS — fed, from the `registry` this function has
    always taken (`_accepted_scope_claims`).
  * CODE, TESTS AND CONFIGURATION — fed, as the findings themselves.
  * the CURRENT OPERATOR REQUEST and CONTEXT RECORDS — represented, not wired:
    there is no operator-request record and no context-record index in this loop
    yet, so a caller that has one passes it as `sources`. That is a stated gap,
    not a silent one — the same distinction `inbox.repo_evidence` draws between
    "nothing was read" and "nothing was found".

THE SEAM WITH intake-01 is unmoved: promotion, deduplication against the tree
and the registry, and the outcome ledger are `inbox.promote_finding`'s. This
module adds only the discipline above, and asks the registry a different
question than `covering_tasks` does — not "is this already actioned" but "do two
sources disagree about which files the work touches".

Priority reflects the mandated order: (1) irreversible data-loss/correctness,
(2) security, (3) dependency ordering (expressed as graph edges, not a rank),
(4) migration/architecture risk, (5) missing regression protection,
(6) maintainability, (7) documentation, (8) optional improvements.

Ids are `au-NNN`, which structurally cannot collide with the repo's existing
roadmap ids (A1–A11 etc.); collisions with the live task registry are checked
explicitly anyway.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..inbox import (
    CLAIM_CONSTRAINT,
    SOURCE_ACCEPTED_DECISION,
    Claim,
    Evidence,
    SourceConflict,
    detect_conflicts,
    unsupported_claims,
)
from ..tasks import TaskRegistry
from .findings import Finding, scope_subject
from .reconcile import ReconciledAudit

_PRIORITY_BY_CATEGORY = {
    "data_loss": 1,
    "defect": 1,
    "security": 2,
    "architecture": 4,
    "missing_test": 5,
    "improvement": 6,
    "doc_drift": 7,
}
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

_RESERVED_ID = re.compile(r"^A\d+$", re.IGNORECASE)


@dataclass(frozen=True)
class ProposedTask:
    id: str
    title: str
    description: str
    priority: int
    depends_on: tuple[str, ...]
    scope: str
    non_goals: str
    acceptance_criteria: tuple[str, ...]
    validation_commands: tuple[str, ...]
    expected_files: tuple[str, ...]
    parallelizable: bool
    finding_ids: tuple[str, ...]

    def to_plan_dict(self) -> dict:
        """The shape ChatGPT re-emits inside a `plan` decision."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "depends_on": list(self.depends_on),
        }

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskGraphProposal:
    tasks: list[ProposedTask] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (finding id, reason)
    #: Every disagreement `detect_conflicts` found, whether or not it stopped
    #: anything. Kept whole, with both sides — a resolved conflict cannot show
    #: an operator what each source actually said.
    conflicts: list[SourceConflict] = field(default_factory=list)
    #: Non-empty when generation STOPPED rather than choosing. The text names
    #: the conflict; `tasks` is then empty by construction.
    stopped: str = ""
    #: What happened to the DURABLE record — written, or why not. Never silent:
    #: a conflict the store could not be told about is still a conflict, and an
    #: operator reading a stop has to know the record they would go looking for
    #: does not exist.
    record_notes: list[str] = field(default_factory=list)


def _title(finding: Finding) -> str:
    text = finding.proposed_action.strip().split("\n")[0]
    return text[:97] + "..." if len(text) > 100 else text


def _stated(values) -> tuple[str, ...]:
    """The entries that actually say something. Blank is ABSENT, which is the
    same reading `Finding.claims` and `findings._str_list` already take."""
    return tuple(v.strip() for v in values if str(v).strip())


def _description(finding: Finding, task: dict, conflicts: tuple = ()) -> str:
    """The task description, carrying everything a FRESH SESSION needs.

    The contract, in order: verified current behaviour WITH its citation;
    desired behaviour; the relevant context ids; the evidence paths and symbols;
    assumptions and unresolved questions; scope and non-goals; the exact
    approved paths; acceptance criteria; validation commands.

    Every repository line is ATTRIBUTED rather than asserted. "as the audit
    cited" is not hedging: the finding is agent-authored, so what this task
    knows is that an agent reported seeing something at a named location — and
    a description that stated it as verified fact would be the loop asserting a
    repository fact it never read, which is the whole thing this task forbids.
    A line with no citation never reaches here at all; `unsupported_claims`
    refuses the finding first.
    """
    # BLANK IS ABSENT, judged exactly as `Finding.claims` judges it. The two
    # have to agree: `claims` skips a whitespace-only `current_behaviour`, so a
    # renderer testing mere truthiness would print a "Current behaviour" heading
    # with nothing under it and no citation — a line no refusal had looked at,
    # which is the one shape this task exists to keep out of a description.
    # (`_str_list` already drops blanks on the parsed path; a `Finding` built in
    # code, as `reconcile._merge` and the tests do, is the way one arrives.)
    behaviour = finding.current_behaviour.strip()
    symbols = _stated(finding.symbols)
    context_ids = _stated(finding.context_ids)
    assumptions = _stated(finding.assumptions)
    open_questions = _stated(finding.open_questions)
    lines = [
        f"Priority: P{task['priority']} | severity {finding.severity} | "
        f"confidence {finding.confidence} | from audit finding {finding.qualified_id}",
    ]
    if behaviour:
        lines += [
            f"Current behaviour, as the audit cited it: {behaviour}",
            f"  read at: {finding.current_behaviour_citation.strip()}",
        ]
    lines += [
        f"Desired behaviour (scope): {finding.proposed_action}",
        "Non-goals: nothing beyond the scope above — no drive-by refactors, "
        "no changes to files outside the expected list without re-approval.",
        f"Impact if unfixed: {finding.impact}",
        f"Evidence the audit cited: {finding.evidence}",
    ]
    if symbols:
        lines.append("Evidence symbols: " + ", ".join(symbols))
    if context_ids:
        lines.append(
            "Context records consulted (REFERENCES, not evidence — verify each "
            "against the tree before believing it): " + ", ".join(context_ids)
        )
    if assumptions:
        lines.append("Assumptions, stated rather than verified: "
                     + "; ".join(assumptions))
    if open_questions:
        lines.append("Unresolved questions: " + "; ".join(open_questions))
    files = ", ".join(finding.affected_files)
    lines += [
        f"Expected files: {files}",
        f"Approved paths this task asks for, exactly: {files}",
    ]
    if finding.acceptance_criteria:
        lines.append("Acceptance criteria: " + "; ".join(finding.acceptance_criteria))
    if finding.validation_commands:
        lines.append("Validation: " + "; ".join(finding.validation_commands))
    lines.append(f"Parallelizable: {'yes' if finding.safe_to_parallelize else 'no'}")
    for conflict in conflicts:
        # A conflict that did NOT change scope. It is carried here as well as in
        # the durable record, because the round that works this task is the one
        # that needs to know two sources disagreed about it — and no winner was
        # picked for it.
        lines.append(f"Recorded source conflict (no winner chosen): {conflict.describe()}")
    return "\n".join(lines)


def _accepted_scope_claims(finding: Finding, registry: TaskRegistry) -> list[Claim]:
    """What the ACCEPTED TASKS AND DECISIONS already say this finding's scope is.

    Matched on the finding's QUALIFIED id and on that alone. A bare finding id
    is often two or three characters (`f1`, `sec-01`), and substring-matching one
    of those against every description in the registry would manufacture
    conflicts out of coincidence — a conflict record that names two sources which
    were never talking about the same thing is worse than no record.

    Retired tasks are skipped: a retired task is not an accepted constraint, it
    is a withdrawn one, and holding a generation up over a decision somebody
    already reversed is the noise that gets a guard switched off.

    Not a duplicate of `inbox.covering_tasks`, which answers whether a finding is
    already ACTIONED so it can be deduplicated. This asks whether the scope that
    was accepted still covers the work the finding says is needed — a different
    question, off the same registry, and the seam intake-01 owns is untouched.
    """
    out: list[Claim] = []
    for task in registry.all_tasks():
        if task.status == "retired":
            continue
        haystack = f"{task.id}\n{task.title}\n{task.description}"
        if finding.qualified_id not in haystack:
            continue
        scope = ", ".join(task.approved_paths) or "(no path — undispatchable)"
        out.append(
            Claim(
                text=f"task {task.id} scopes this work to {scope}",
                source=SOURCE_ACCEPTED_DECISION,
                subject=scope_subject(finding.qualified_id),
                kind=CLAIM_CONSTRAINT,
                citation=Evidence(
                    text=f"{task.id}.approved_paths = {scope}", source="tasks.json"
                ),
                paths=tuple(task.approved_paths),
            )
        )
    return out


def _record_conflicts(
    conflicts, blocker_store, now: str, proposal: TaskGraphProposal
) -> None:
    """Persist every conflict through `BlockerStore`, and SAY what happened.

    Called AFTER the stop decision has already been made, never as part of
    making it. A store that raises must not be able to skip a stop — that is the
    fail-open this ordering exists to prevent: the alarm would go unrecorded and
    generation would carry on as though nothing had disagreed.

    One record per `SourceConflict.identity`, which is what keeps two different
    disagreements from collapsing into one record whose text is whichever was
    written last (`blockers.planning_conflict_phase` states that in full).
    """
    from ..blockers import record_planning_conflict
    from ..errors import StateCorruptError, StateError

    if not conflicts:
        return
    if blocker_store is None:
        proposal.record_notes.append(
            f"{len(conflicts)} source conflict(s) were NOT recorded durably: this "
            "generation was given no blocker store. The conflicts are reported "
            "here and in the task descriptions only."
        )
        return
    for conflict in conflicts:
        try:
            blocker = record_planning_conflict(
                blocker_store,
                identity=conflict.identity,
                question=(
                    "Two sources disagree and planning refused to choose between "
                    f"them: {conflict.describe()}"
                ),
                detail=(
                    f"subject: {conflict.subject}\n"
                    f"scope impact: {conflict.scope_impact}\n"
                    f"source A ({conflict.left.source}): {conflict.left.describe()}\n"
                    f"  scope A: {', '.join(conflict.left.paths) or 'unstated'}\n"
                    f"source B ({conflict.right.source}): {conflict.right.describe()}\n"
                    f"  scope B: {', '.join(conflict.right.paths) or 'unstated'}"
                ),
                now=now,
            )
        except (OSError, StateError, StateCorruptError) as exc:
            proposal.record_notes.append(
                f"conflict {conflict.identity} could NOT be recorded durably "
                f"({exc}) — it still stopped or was reported, but no blocker "
                "record exists to answer"
            )
        else:
            proposal.record_notes.append(
                f"conflict {conflict.identity} recorded as {blocker.id}"
            )


def generate_tasks(
    reconciled: ReconciledAudit,
    registry: TaskRegistry,
    *,
    sources=(),
    blocker_store=None,
    now: str = "",
) -> TaskGraphProposal:
    """Findings → a proposed task graph, with ctx-06's planning discipline.

    `sources` are extra `inbox.Claim`s from the tiers this loop has no live feed
    for — the operator's request and the context records. A conflict is only
    DETECTED between claims that name the same subject, so such a claim has to
    use `findings.scope_subject(finding.qualified_id)` to be compared with the
    finding's own scope claim; that function exists to be the one spelling.
    `blocker_store` is where a conflict is durably recorded; without one,
    conflicts are still detected, still stop generation and still reported, and
    `record_notes` says the durable record was not made.

    Both are keyword-only with defaults, so every existing caller is unchanged:
    with no `sources` the tiers that ARE fed (the findings, and the registry
    passed as the second argument) are still compared with each other.

    A `sources` claim is a PARTY to a conflict and is never asserted by a
    generated task, so it is deliberately not held to the citation rule: an
    uncited operator sentence that contradicts the tree is exactly what has to
    be recorded, and refusing it here would DROP it — turning the fail-closed
    direction into a fail-open one. What it is backed by travels with it into
    the record, through `Claim.describe`.
    """
    from ..state import utcnow_iso

    proposal = TaskGraphProposal()
    now = now or utcnow_iso()
    promotable = sorted(
        reconciled.promotable(),
        key=lambda f: (
            _PRIORITY_BY_CATEGORY.get(f.category, 8),
            _SEVERITY_RANK.get(f.severity, 9),
            f.qualified_id,
        ),
    )

    # ---- refuse the findings whose claims nobody read ----------------------
    #
    # BEFORE ids are assigned, so a refused finding does not consume an `au-NNN`
    # and does not have to be unwound afterwards.
    supported: list[Finding] = []
    for finding in promotable:
        refusals = unsupported_claims(finding.claims())
        if refusals:
            proposal.skipped.append(
                (
                    finding.qualified_id,
                    "refused — a repository claim with no citation and no stated "
                    "assumption: " + "; ".join(refusals),
                )
            )
            continue
        supported.append(finding)

    # ---- do the sources disagree? -----------------------------------------
    claims = list(sources)
    for finding in supported:
        claims += list(finding.claims())
        claims += _accepted_scope_claims(finding, registry)
    conflicts = list(detect_conflicts(claims))
    proposal.conflicts = conflicts
    stopping = [c for c in conflicts if c.stops_generation]

    # THE STOP IS DECIDED HERE, before anything is written. `_record_conflicts`
    # below can fail without changing this line's answer.
    if stopping:
        proposal.stopped = (
            f"{len(stopping)} source conflict(s) bear on SCOPE, so no task was "
            "generated and no source was preferred over another: "
            + " | ".join(c.describe() for c in stopping)
        )
        for conflict in stopping:
            proposal.skipped.append(
                (
                    conflict.subject or "(no subject)",
                    "generation stopped — " + conflict.describe(),
                )
            )
        _record_conflicts(conflicts, blocker_store, now, proposal)
        return proposal
    _record_conflicts(conflicts, blocker_store, now, proposal)

    by_subject: dict[str, list[SourceConflict]] = {}
    for conflict in conflicts:
        by_subject.setdefault(conflict.subject, []).append(conflict)

    promotable = supported

    # First pass: assign ids in priority order.
    id_by_finding: dict[str, str] = {}
    seq = 1
    assigned: list[tuple[str, Finding]] = []
    for finding in promotable:
        while True:
            candidate = f"au-{seq:03d}"
            seq += 1
            if not registry.has(candidate) and not _RESERVED_ID.match(candidate):
                break
        id_by_finding[finding.qualified_id] = candidate
        # Agents reference dependencies by their local id; map both forms.
        id_by_finding.setdefault(finding.id, candidate)
        assigned.append((candidate, finding))

    # Second pass: resolve dependencies finding-id -> task-id.
    for task_id, finding in assigned:
        deps: list[str] = []
        unresolved: list[str] = []
        for dep in finding.dependencies:
            mapped = id_by_finding.get(dep)
            if mapped and mapped != task_id:
                deps.append(mapped)
            elif registry.has(dep):
                deps.append(dep)  # dependency on an existing roadmap task
            else:
                unresolved.append(dep)
        priority = _PRIORITY_BY_CATEGORY.get(finding.category, 8)
        task = {
            "priority": priority,
        }
        description = _description(
            finding,
            task,
            conflicts=tuple(by_subject.get(scope_subject(finding.qualified_id), ())),
        )
        if unresolved:
            description += (
                f"\nNote: unresolved dependency references from the audit: {unresolved}"
            )
        proposal.tasks.append(
            ProposedTask(
                id=task_id,
                title=_title(finding),
                description=description,
                priority=priority,
                depends_on=tuple(dict.fromkeys(deps)),
                scope=finding.proposed_action,
                non_goals=(
                    "Nothing beyond the stated scope; no changes outside the "
                    "expected files without re-approval."
                ),
                acceptance_criteria=finding.acceptance_criteria,
                validation_commands=finding.validation_commands,
                expected_files=finding.affected_files,
                parallelizable=finding.safe_to_parallelize,
                finding_ids=(finding.qualified_id,),
            )
        )

    for bucket_finding in reconciled.buckets["human_decisions"]:
        proposal.skipped.append(
            (bucket_finding.qualified_id, "human decision — surfaced to the operator, not a task")
        )
    return proposal
