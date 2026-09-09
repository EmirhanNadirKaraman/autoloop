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
    citation naming a real reader or a stated assumption — and where that reader
    is the audit report, whose content an agent wrote, the cited location must be
    one a `TreeReader` actually saw. A claim carrying neither, or citing a path
    nobody has, is REFUSED, the finding becomes no task, and the refusal names
    the claim — in `TaskGraphProposal.skipped`, which `audit/report.py` already
    renders for the operator.
  * a claim that is NOT supported is never restated in a generated task, whoever
    made it. An unsupported source is still compared and still recorded (dropping
    it would hide the disagreement), but the description carries the refusal in
    place of the sentence — `inbox.describe_conflict_for_task`.
  * a disagreement between two sources about one subject is RECORDED durably
    through `blockers.BlockerStore` (a park record, not a field in
    `state.json`, so it survives a set-aside and a reset and can be answered
    later). A conflict that changes SCOPE stops generation; one that provably
    does not is recorded and generation continues — and "recorded" means the
    durable record EXISTS. A conflict that could not be written is stopped on
    too, because "recorded and continued" with no record is the fail-open this
    discipline is for.

WHICH TIERS ARE ACTUALLY FED, said plainly, because "represented" and "wired"
are different and only one of them is a guarantee:

  * ACCEPTED TASKS AND DECISIONS — fed, from the `registry` this function has
    always taken (`_accepted_scope_claims`), one claim per task so two accepted
    tasks can disagree with each other and not merely with the finding.
  * CODE, TESTS AND CONFIGURATION — fed, as the findings themselves, and their
    citations are checked against a real `git ls-files` of the checkout.
  * the CURRENT OPERATOR REQUEST — fed, from the operator's own intake drafts
    (`inbox.draft_scope_claims`), which is the one artifact in this loop carrying
    an operator's words before they become an accepted decision.
  * CONTEXT RECORDS — NOT fed, and this is the one gap left. No context-record
    index is wired into this loop at any call site (`context_packet.py` says the
    same of the packet, and `docs/SCHEMA.md` records that ctx-03 fixed the record
    SHAPE and deliberately not its location). It is a STATED gap: the wiring
    layer passes that sentence down as a note and the generator renders it, so a
    reader of an audit report is told the tier said nothing because nothing reads
    it — never left to infer agreement from silence. One argument away: a caller
    that has an index adds its claims through the same provider.

**HOW PRODUCTION GETS THEM, since `audit/executor.py:803` calls
`generate_tasks(reconciled, self._registry)` and hands this function nothing
else.** That signature is not ctx-06's to change — the executor is outside this
task's approved paths — so the inputs ride on the object the CLI's wiring layer
already chooses and the executor already passes: `cli._build_executor` attaches
an `inbox.PlanningSources` to the registry (a blocker store on
`config.blockers_dir`, a lazy `TreeReader` over the checkout, the intake-draft
provider, and the context-record note), and `generate_tasks` reads it through
`inbox.planning_sources_of`. Explicit keyword arguments still win, so every
existing caller and every test that states its own inputs is unchanged.

That is what makes "record it and continue" real on the audit path rather than a
branch nothing reaches: with a store present, a conflict that provably does not
change scope is filed and generation carries on. An unrecorded conflict still
STOPS — see `_record_conflicts` — because "recorded and continued" with no record
is the fail-open this discipline exists for, but that branch is now the
degenerate case (a store that failed) rather than the normal one.

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
    describe_conflict_for_task,
    detect_conflicts,
    mention_pattern,
    planning_sources_of,
    resolve_tree,
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


def _description(finding: Finding, task: dict, conflicts: tuple = (), tree=None) -> str:
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
    refuses the finding first, and for a claim about CURRENT BEHAVIOUR that
    refusal now includes a citation whose location no tree read confirmed.

    EVERY SENTENCE BELOW TRACES TO A CLAIM THAT WENT THROUGH THAT GATE —
    `evidence`, `proposed_action`, `impact`, `current_behaviour` and each
    assumption are all in `Finding.claims`, and a conflict is rendered through
    `inbox.describe_conflict_for_task`, which withholds any side that did not.
    The deliberate exception is `open_questions`: a question asserts nothing, and
    it is rendered under a heading that says it is unsettled. The property is
    worth stating because it is what a future field can break silently — a line
    added here with no claim behind it is a repository statement no refusal ever
    looked at.
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
    ]
    if finding.impact.strip():
        # BLANK IS ABSENT here too, for the reason the behaviour line above gives
        # and for one more: `Finding.claims` emits no claim for an impact nobody
        # wrote, so a heading printed anyway would be a line in the task that no
        # refusal had ever looked at.
        lines.append(f"Impact if unfixed, as the audit stated it: {finding.impact}")
    lines.append(f"Evidence the audit cited: {finding.evidence}")
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
        #
        # RENDERED THROUGH THE PROJECTION, not through `SourceConflict.describe`.
        # A `sources` claim is a party to a conflict whatever backs it (dropping
        # an uncited one would hide the disagreement), and a task description is
        # read by a fresh session that cannot tell a quoted source from a fact —
        # so an unsupported side's SENTENCE is withheld here while both sources,
        # both scopes and the rule it failed are named. The verbatim account is
        # in the durable record, which is written for an operator.
        lines.append(
            "Recorded source conflict (no winner chosen): "
            + describe_conflict_for_task(conflict, tree=tree)
        )
    return "\n".join(lines)


def _mentions(qualified_id: str) -> re.Pattern:
    """Matches the finding id where it is WRITTEN, not where it is contained.

    `inbox.mention_pattern` holds the rule and the reasoning; this name stays
    because the two call sites below read better with it. ONE spelling, because
    the registry and the operator's drafts are asked the same question ("does
    this text name that finding") and two patterns would answer it differently —
    which is a conflict found against one source and missed against the other.
    """
    return mention_pattern(qualified_id)


def _accepted_scope_claims(finding: Finding, registry: TaskRegistry) -> list[Claim]:
    """What the ACCEPTED TASKS AND DECISIONS already say this finding's scope is.

    Matched on the finding's QUALIFIED id and on that alone, at a word boundary
    (`_mentions`). A bare finding id is often two or three characters (`f1`,
    `sec-01`), and substring-matching one of those against every description in
    the registry would manufacture conflicts out of coincidence — a conflict
    record that names two sources which were never talking about the same thing
    is worse than no record.

    ONE CLAIM PER TASK, each carrying that task's id as its `author`, so two
    accepted tasks that scope one finding differently are compared with EACH
    OTHER and not merely with the finding. Sharing a tier is not agreeing, and
    `detect_conflicts` can only see that if the claims say who made them.

    The claim's TEXT deliberately does not name the task. Two tasks that scope
    the work identically would then differ in words while agreeing in substance,
    and the word comparison would report a conflict between two sources that
    agree. The id belongs to the author field and to the citation, both of which
    reach the operator through `Claim.describe`.

    Retired tasks are skipped: a retired task is not an accepted constraint, it
    is a withdrawn one, and holding a generation up over a decision somebody
    already reversed is the noise that gets a guard switched off.

    Not a duplicate of `inbox.covering_tasks`, which answers whether a finding is
    already ACTIONED so it can be deduplicated. This asks whether the scope that
    was accepted still covers the work the finding says is needed — a different
    question, off the same registry, and the seam intake-01 owns is untouched.
    """
    out: list[Claim] = []
    mention = _mentions(finding.qualified_id)
    for task in registry.all_tasks():
        if task.status == "retired":
            continue
        haystack = f"{task.id}\n{task.title}\n{task.description}"
        if not mention.search(haystack):
            continue
        scope = ", ".join(task.approved_paths) or "(no path — undispatchable)"
        out.append(
            Claim(
                text=f"this work is scoped to {scope}",
                source=SOURCE_ACCEPTED_DECISION,
                author=task.id,
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
) -> list:
    """Persist every conflict through `BlockerStore`. Returns the ones with NO
    durable record, and says what happened to each either way.

    THE ORDERING INVARIANT, restated because it changed: the scope-based stop is
    computed by the caller BEFORE this runs, and what this returns can only ADD a
    stop, never cancel one. A store that fails therefore cannot talk generation
    into proceeding — the direction the old "record after deciding" split existed
    to protect, kept, while an unrecordable conflict now also stops.

    TOTAL BY CONSTRUCTION. The store is caller-supplied, so the exception net is
    wide on purpose: anything a store raises becomes an UNRECORDED conflict —
    which stops — rather than an exception escaping `generate_tasks` and taking
    down the audit report that would have told the operator what disagreed. A
    narrower net reads as more disciplined and fails in the worse direction.

    One record per `SourceConflict.identity`, which is what keeps two different
    disagreements from collapsing into one record whose text is whichever was
    written last (`blockers.planning_conflict_phase` states that in full).
    """
    from ..blockers import record_planning_conflict

    if not conflicts:
        return []
    if blocker_store is None:
        proposal.record_notes.append(
            f"{len(conflicts)} source conflict(s) were NOT recorded durably: this "
            "generation was given no blocker store, so there is nothing for an "
            "operator to list or answer later."
        )
        return list(conflicts)
    unrecorded = []
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
        except Exception as exc:  # noqa: BLE001 — see TOTAL BY CONSTRUCTION above
            unrecorded.append(conflict)
            proposal.record_notes.append(
                f"conflict {conflict.identity} could NOT be recorded durably "
                f"({type(exc).__name__}: {exc}) — no blocker record exists to "
                "answer, so generation stops on it rather than continuing"
            )
        else:
            proposal.record_notes.append(
                f"conflict {conflict.identity} recorded as {blocker.id}"
            )
    return unrecorded


def _provided_sources(sources, planning, supported) -> tuple[tuple, tuple[str, ...]]:
    """`(claims, notes)` from the caller's `sources` or the wired provider.

    AN EXPLICIT ARGUMENT WINS, including an explicitly empty one: a test or a
    caller that states its own sources is saying what the tiers are, and a
    provider silently adding more would make that statement untrue. `None` — the
    default — means "use whatever this deployment wired", which is how the audit
    path gets the operator's drafts without `audit/executor.py` knowing they
    exist.

    TOTAL, like `_record_conflicts` and for the same reason: the provider is
    caller-supplied, and anything it raises becomes a NOTE saying the tier was
    not read, rather than an exception ending a generation that has findings to
    report. A tier that could not be read is reported, never counted as agreement
    — the whole point of `repo_evidence`'s "nothing was read" sentence, applied
    one level up.
    """
    if sources is not None:
        return tuple(sources), ()
    provider = planning.provider
    if provider is None:
        return (), ()
    try:
        produced, notes = provider(supported)
        claims = tuple(produced)
        notes = tuple(str(note) for note in notes)
        # SHAPE-CHECKED HERE, not left to explode downstream. Anything that is
        # not a `Claim` reaches `unsupported_claims` as an object with no `.text`
        # and takes the whole generation down with an `AttributeError` — a
        # provider returning the wrong shape has to read as a tier that was not
        # read, which is the same answer as a provider that raised.
        wrong = [c for c in claims if not isinstance(c, Claim)]
        if wrong:
            raise TypeError(f"{len(wrong)} of {len(claims)} entries are not Claims")
    except Exception as exc:  # noqa: BLE001 — a broken provider must not end the run
        return (), (
            f"the wired source provider failed ({type(exc).__name__}: {exc}): "
            "NOTHING WAS READ from the tiers it feeds, so any disagreement they "
            "hold with this audit is UNKNOWN rather than absent.",
        )
    return claims, notes


def generate_tasks(
    reconciled: ReconciledAudit,
    registry: TaskRegistry,
    *,
    sources=None,
    blocker_store=None,
    tree=None,
    now: str = "",
) -> TaskGraphProposal:
    """Findings → a proposed task graph, with ctx-06's planning discipline.

    EVERY INPUT BELOW HAS TWO ROUTES: the keyword argument, and the
    `inbox.PlanningSources` the deployment attached to `registry` (see the module
    docstring for why the seam is there and not in a signature this task may not
    change). The keyword wins wherever it is given, so a caller that states its
    inputs — every test in `test_audit_taskgen.py` — is unaffected by what a
    deployment wired.

    `sources` are `inbox.Claim`s from the tiers that are not the findings and not
    the registry: the operator's request today, a context-record index when one
    is wired. A conflict is only DETECTED between claims that name the same
    subject, so such a claim has to use `findings.scope_subject(qualified_id)` to
    be compared with the finding's own scope claim; that function exists to be
    the one spelling.

    `blocker_store` is where a conflict is durably recorded, and `tree` is the
    `inbox.TreeReader` a model-authored citation is checked against. Both absent
    means both refusals: a citation that cannot be verified is refused, and a
    conflict that cannot be recorded stops generation, because "we could not
    check" must never produce the same outcome as "we checked".

    A `sources` claim is a PARTY to a conflict whatever backs it, and it is
    VALIDATED but never dropped. Dropping an uncited operator sentence that
    contradicts the tree would hide exactly the disagreement worth recording —
    fail-closed turned fail-open — so the refusal is reported into `skipped`, the
    claim still travels into the comparison and the durable record, and
    `inbox.describe_conflict_for_task` is what keeps its words out of any task
    description.
    """
    from ..state import utcnow_iso

    proposal = TaskGraphProposal()
    now = now or utcnow_iso()
    planning = planning_sources_of(registry)
    if blocker_store is None:
        blocker_store = planning.blocker_store
    tree = resolve_tree(tree if tree is not None else planning.tree)
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
        refusals = unsupported_claims(finding.claims(), tree=tree)
        if refusals:
            proposal.skipped.append(
                (
                    finding.qualified_id,
                    "refused — a repository claim with no citation, no verified "
                    "location and no stated assumption: " + "; ".join(refusals),
                )
            )
            continue
        supported.append(finding)

    # ---- what the other tiers say, and what backs it ----------------------
    source_claims, source_notes = _provided_sources(sources, planning, supported)
    for note in (*planning.notes, *source_notes):
        # SAID IN THE REPORT, not only in `record_notes`, which nothing renders.
        # A tier with no producer, a draft that would not open, a reader that
        # answered with nothing — each is a thing this generation did NOT compare,
        # and an operator reading a clean proposal has to be able to tell that
        # apart from a comparison that found nothing to report.
        proposal.record_notes.append(note)
        proposal.skipped.append(("(planning sources)", note))
    for refusal in unsupported_claims(source_claims, tree=tree):
        proposal.skipped.append(
            (
                "(planning sources)",
                "an unsupported source claim was NOT asserted and NOT dropped — "
                "it is compared and recorded, and no task repeats it: " + refusal,
            )
        )

    # ---- do the sources disagree? -----------------------------------------
    claims = list(source_claims)
    for finding in supported:
        claims += list(finding.claims())
        claims += _accepted_scope_claims(finding, registry)
    conflicts = list(detect_conflicts(claims))
    proposal.conflicts = conflicts

    # THE SCOPE STOP IS DECIDED HERE, before anything is written, so a store that
    # fails cannot talk generation into proceeding.
    stopping = {id(c) for c in conflicts if c.stops_generation}
    unrecorded = {id(c) for c in _record_conflicts(conflicts, blocker_store, now, proposal)}

    # A CONFLICT NOBODY CAN LOOK UP LATER IS NOT A CONFLICT THAT WAS HANDLED.
    # "Record it and continue" is only honest when the record exists: this task's
    # requirement is a DURABLE, operator-facing record, and a note in a proposal
    # object that the audit report does not even render is neither. So an
    # unrecorded conflict joins the stop rather than being waved through — the
    # fail-open this would otherwise be is precisely "the alarm did not fire and
    # nothing said so". Recording can only ADD to `halting`, never remove from
    # it, so the ordering above still holds.
    halting = [c for c in conflicts if id(c) in stopping | unrecorded]

    if halting:
        scope_stops = [c for c in halting if id(c) in stopping]
        record_stops = [c for c in halting if id(c) not in stopping]
        why = []
        if scope_stops:
            why.append(f"{len(scope_stops)} bear(s) on SCOPE")
        if record_stops:
            why.append(f"{len(record_stops)} could not be recorded durably")
        proposal.stopped = (
            f"{len(halting)} source conflict(s) stopped generation "
            f"({'; '.join(why)}), so no task was generated and no source was "
            "preferred over another: "
            + " | ".join(c.describe() for c in halting)
        )
        for conflict in halting:
            # Into `skipped`, which is the ONE field of this proposal that
            # `audit/report.py` renders — so the disagreement, and the absence of
            # a record for it, reach the operator's report rather than living in
            # an object nobody prints.
            if id(conflict) in stopping:
                reason = "generation stopped — " + conflict.describe()
            else:
                reason = (
                    "generation stopped — this conflict does not change scope, "
                    "but no durable record of it could be made, so it was not "
                    "passed over silently: " + conflict.describe()
                )
            if id(conflict) in unrecorded:
                # SAID IN THE REPORT, not only in `record_notes`, which nothing
                # renders. An operator told a conflict stopped generation will go
                # looking for the blocker to answer, and has to be told in the
                # same breath that there is none to find.
                reason += (
                    " NO DURABLE RECORD EXISTS for this conflict — `python -m "
                    "autoloop blockers` will not list it; whether that is a "
                    "generation with no store or a store that failed to write is "
                    "in this proposal's record notes."
                )
            proposal.skipped.append((conflict.subject or "(no subject)", reason))
        return proposal

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
            tree=tree,
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
