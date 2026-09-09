"""Task-graph proposal generation: priority ordering, id assignment without
collisions, dependency mapping, human decisions excluded — and (ctx-06) the
planning discipline layered on top: an uncited repository claim is refused by
name, and a source conflict is recorded rather than silently decided."""

from dataclasses import replace

from test_audit_reconcile import finding

from autoloop.audit.reconcile import reconcile
from autoloop.audit.taskgen import generate_tasks
from autoloop.blockers import PLANNING_SOURCE_CONFLICT, BlockerStore
from autoloop.inbox import (
    CLAIM_BEHAVIOUR,
    CLAIM_CONSTRAINT,
    PLANNING_SOURCES_ATTR,
    SCOPE_UNCHANGED,
    SOURCE_CONTEXT_RECORD,
    SOURCE_OPERATOR_REQUEST,
    SOURCE_REPOSITORY,
    Claim,
    Evidence,
    PlanningSources,
    TreeReader,
    attach_planning_sources,
)
from autoloop.tasks import Task, TaskRegistry

#: THE TREE these findings are checked against. Every finding here comes from
#: `test_audit_reconcile.finding`, whose evidence cites `a.py:12`, and `cited()`
#: below adds `autoloop/policy.py:120` — so those two paths are the whole tree,
#: deliberately: a permissive reader that held everything would verify nothing
#: and the refusal tests below would pass for the wrong reason.
TREE = TreeReader.of_paths(
    ("a.py", "autoloop/policy.py"), source="the tree this test states"
)


def with_tree(registry, tree=TREE, **extra):
    """The registry, carrying the planning seam a deployment attaches.

    THE PRODUCTION ROUTE (`cli._build_executor` → `AuditExecutor` →
    `generate_tasks(reconciled, self._registry)`), used here rather than the
    keyword argument wherever a test does not care which route it took — a seam
    only tests bypass is a seam production does not have.
    """
    return attach_planning_sources(registry, PlanningSources(tree=tree, **extra))


def registry(*ids):
    return with_tree(TaskRegistry([Task(id=i, title="t", description="d") for i in ids]))


def test_priority_ordering():
    result = reconcile(
        [
            finding("doc", category="doc_drift", files=("d.md",)),
            finding("loss", category="data_loss", files=("a.py",)),
            finding("sec", category="security", files=("b.py",)),
            finding("test", category="missing_test", files=("c.py",)),
        ]
    )
    proposal = generate_tasks(result, registry())
    priorities = [(t.finding_ids[0].split(":")[1], t.priority) for t in proposal.tasks]
    assert priorities == [("loss", 1), ("sec", 2), ("test", 5), ("doc", 7)]
    # ids assigned in priority order
    assert [t.id for t in proposal.tasks] == ["au-001", "au-002", "au-003", "au-004"]


def test_ids_skip_registry_collisions():
    result = reconcile([finding("f1")])
    proposal = generate_tasks(result, registry("au-001", "au-002"))
    assert proposal.tasks[0].id == "au-003"


def test_never_generates_reserved_roadmap_ids():
    result = reconcile([finding(f"f{i}", files=(f"x{i}.py",)) for i in range(5)])
    proposal = generate_tasks(result, registry())
    assert all(t.id.startswith("au-") for t in proposal.tasks)


def test_dependency_mapping_between_findings():
    result = reconcile(
        [
            finding("base", category="defect", files=("a.py",)),
            finding("follow", category="missing_test", files=("b.py",), deps=("base",)),
        ]
    )
    proposal = generate_tasks(result, registry())
    by_finding = {t.finding_ids[0].split(":")[1]: t for t in proposal.tasks}
    assert by_finding["follow"].depends_on == (by_finding["base"].id,)


def test_dependency_on_existing_roadmap_task_preserved():
    result = reconcile([finding("f1", deps=("A2",))])
    proposal = generate_tasks(result, registry("A2"))
    assert proposal.tasks[0].depends_on == ("A2",)


def test_unresolved_dependency_noted_not_invented():
    result = reconcile([finding("f1", deps=("ghost-finding",))])
    proposal = generate_tasks(result, registry())
    task = proposal.tasks[0]
    assert task.depends_on == ()
    assert "unresolved dependency" in task.description


def test_human_decisions_are_skipped_with_reason():
    result = reconcile([finding("hd", category="human_decision")])
    proposal = generate_tasks(result, registry())
    assert proposal.tasks == []
    assert proposal.skipped and "human decision" in proposal.skipped[0][1]


def test_task_carries_full_structure():
    result = reconcile([finding("f1")])
    [task] = generate_tasks(result, registry()).tasks
    assert task.scope == "fix it"
    assert "no drive-by refactors" in task.description
    assert task.acceptance_criteria == ("fixed",)
    assert task.validation_commands == ("ruff check .",)
    assert task.expected_files == ("a.py",)
    assert task.parallelizable is True
    assert task.to_plan_dict()["id"] == task.id
    assert "Desired behaviour (scope):" in task.to_plan_dict()["description"]


# ---- ctx-06: no uncited repository claim, and no silent choice -------------


def cited(fid="f1", **overrides):
    """A finding that carries a citation for everything it asserts."""
    base = dict(
        current_behaviour="the gate returns True when the file is unreadable",
        current_behaviour_citation="autoloop/policy.py:120",
    )
    base.update(overrides)
    return replace(finding(fid), **base)


def test_a_repository_claim_with_no_citation_is_refused_and_named():
    """The claim, at its narrowest: state what the code does today, decline to
    say where you read it, and the finding becomes no task — with the offending
    claim quoted in the refusal."""
    uncited = replace(
        finding("f1"),
        current_behaviour="the gate returns True when the file is unreadable",
        current_behaviour_citation="",
    )
    proposal = generate_tasks(reconcile([uncited]), registry())

    assert proposal.tasks == []
    [(finding_id, reason)] = proposal.skipped
    assert finding_id == "d1:f1"
    assert "the gate returns True when the file is unreadable" in reason
    assert "no citation" in reason


def test_the_same_claim_stated_as_an_assumption_is_accepted():
    """The other half of the rule, and the half that keeps it usable: an agent
    that cannot cite something says so out loud, and the task carries it as an
    assumption rather than as a fact."""
    honest = replace(
        finding("f1"),
        assumptions=("the gate probably returns True when the file is unreadable",),
    )
    [task] = generate_tasks(reconcile([honest]), registry()).tasks

    assert "Assumptions, stated rather than verified" in task.description
    assert "probably returns True" in task.description


def test_evidence_that_cites_nowhere_is_refused():
    """`evidence` is specified as file:line references. A finding whose evidence
    names no location has cited nothing, whatever its confidence says."""
    uncited = replace(finding("f1"), evidence="I saw it")
    proposal = generate_tasks(reconcile([uncited]), registry())

    assert proposal.tasks == []
    assert "no location" in proposal.skipped[0][1]


def test_a_cited_claim_is_carried_with_its_citation():
    [task] = generate_tasks(reconcile([cited()]), registry()).tasks

    assert "Current behaviour, as the audit cited it:" in task.description
    assert "autoloop/policy.py:120" in task.description


def test_a_scope_conflict_stops_generation_and_names_both_sources(tmp_path):
    """An accepted task whose scope CANNOT reach a file the finding says must
    change. Generation stops, both sources are named, and neither wins."""
    store = BlockerStore(tmp_path / "blockers")
    accepted = TaskRegistry([
        Task(
            id="already",
            title="t",
            description="covers d1:f1",
            approved_paths=("b.py",),   # the finding needs a.py
        )
    ])
    proposal = generate_tasks(
        reconcile([finding("f1")]), accepted, blocker_store=store, tree=TREE,
        now="2026-09-09T00:00:00Z",
    )

    assert proposal.tasks == []
    assert proposal.stopped
    assert "scope_changed" in proposal.stopped
    # BOTH sources named, no winner.
    assert "accepted_decision" in proposal.stopped and "repository" in proposal.stopped
    assert "No winner was chosen" in proposal.stopped

    [blocker] = store.open_blockers()
    assert blocker.code == PLANNING_SOURCE_CONFLICT
    assert "accepted_decision" in blocker.detail and "repository" in blocker.detail
    assert "b.py" in blocker.detail and "a.py" in blocker.detail


def test_an_accepted_scope_that_already_covers_the_work_does_not_stop_anything():
    """The direction that would make the guard useless if it fired: a WIDER
    accepted scope is not a disagreement, it is room to work."""
    accepted = TaskRegistry([
        Task(id="already", title="t", description="covers d1:f1",
             approved_paths=("a.py", "autoloop/tests/")),
    ])
    proposal = generate_tasks(reconcile([finding("f1")]), accepted, tree=TREE)

    assert proposal.stopped == ""
    assert [t.finding_ids for t in proposal.tasks] == [("d1:f1",)]


def test_a_conflict_that_does_not_change_scope_is_recorded_and_generation_continues(tmp_path):
    """Two sources disagreeing about something that is not scope. The task is
    still generated, the disagreement is still durable, and still unresolved."""
    store = BlockerStore(tmp_path / "blockers")
    # The operator asserting CURRENT BEHAVIOUR — the same question the finding
    # answers, so their words are compared — while agreeing about which file is
    # involved. Precedence says the request is not evidence of behaviour, and
    # the answer to that is a record, never a choice.
    operator = Claim(
        text="the gate already returns False there",
        source=SOURCE_OPERATOR_REQUEST,
        subject="d1:f1",
        kind=CLAIM_BEHAVIOUR,
        citation=Evidence(text="the request", source="the operator's request"),
        paths=("a.py",),          # the SAME paths the finding names
    )
    proposal = generate_tasks(
        reconcile([finding("f1")]),
        registry(),
        sources=[operator],
        blocker_store=store,
        now="2026-09-09T00:00:00Z",
    )

    assert proposal.stopped == ""
    assert len(proposal.tasks) == 1
    assert len(proposal.conflicts) == 1
    assert proposal.conflicts[0].scope_impact == SCOPE_UNCHANGED
    [blocker] = store.open_blockers()
    assert blocker.code == PLANNING_SOURCE_CONFLICT
    # And the round that works the task is told about it.
    assert "Recorded source conflict (no winner chosen)" in proposal.tasks[0].description


def test_two_different_conflicts_are_two_records_not_one(tmp_path):
    """`BlockerStore.record` keys on (task, code, phase) and a bump REPLACES the
    text. Filed under one phase, the second conflict would erase the first
    one's account of who disagreed — which is the whole content of the record."""
    store = BlockerStore(tmp_path / "blockers")
    accepted = TaskRegistry([
        Task(id="one", title="t", description="covers d1:f1", approved_paths=("z.py",)),
        Task(id="two", title="t", description="covers d1:f2", approved_paths=("z.py",)),
    ])
    proposal = generate_tasks(
        reconcile([finding("f1", files=("a.py",)), finding("f2", files=("b.py",))]),
        accepted,
        blocker_store=store,
        tree=TREE,
        now="2026-09-09T00:00:00Z",
    )

    assert proposal.stopped
    assert len(store.open_blockers()) == 2
    details = "\n".join(b.detail for b in store.open_blockers())
    assert "a.py" in details and "b.py" in details


def test_a_store_that_cannot_be_written_does_not_cancel_the_stop(tmp_path):
    """The fail-open this ordering exists to prevent: if recording and stopping
    shared a `try`, a store that raised would skip the stop and generation would
    carry on as though nothing had disagreed."""

    class BrokenStore(BlockerStore):
        def record(self, **kwargs):
            raise OSError("disk is gone")

    store = BrokenStore(tmp_path / "blockers")
    accepted = TaskRegistry([
        Task(id="already", title="t", description="covers d1:f1", approved_paths=("b.py",))
    ])
    proposal = generate_tasks(
        reconcile([finding("f1")]), accepted, blocker_store=store, tree=TREE
    )

    assert proposal.tasks == []
    assert proposal.stopped
    assert any("could NOT be recorded durably" in note for note in proposal.record_notes)


def test_no_store_at_all_still_stops_and_says_nothing_was_recorded():
    accepted = TaskRegistry([
        Task(id="already", title="t", description="covers d1:f1", approved_paths=("b.py",))
    ])
    proposal = generate_tasks(reconcile([finding("f1")]), accepted, tree=TREE)

    assert proposal.tasks == []
    assert proposal.stopped
    assert any("NOT recorded durably" in note for note in proposal.record_notes)


def test_two_accepted_tasks_that_scope_one_finding_differently_disagree(tmp_path):
    """Sharing a tier is not agreeing. Two ACCEPTED TASKS can scope one finding
    to two different file sets, and suppressing that because both read
    `accepted_decision` hid the conflict an operator actually has to settle."""
    store = BlockerStore(tmp_path / "blockers")
    accepted = TaskRegistry([
        Task(id="one", title="t", description="covers d1:f1", approved_paths=("a.py",)),
        Task(id="two", title="t", description="covers d1:f1",
             approved_paths=("a.py", "b.py", "c.py")),
    ])
    proposal = generate_tasks(
        reconcile([finding("f1")]), accepted, blocker_store=store, tree=TREE,
        now="2026-09-09T00:00:00Z",
    )

    assert proposal.tasks == []
    assert proposal.stopped
    # BOTH tasks named — the source is identical on both sides of this one, so a
    # record naming only the tier would name neither of them.
    pair = [
        c for c in proposal.conflicts
        if {c.left.author, c.right.author} == {"one", "two"}
    ]
    assert len(pair) == 1, [c.describe() for c in proposal.conflicts]
    details = "\n".join(b.detail for b in store.open_blockers())
    assert "accepted_decision: one" in details
    assert "accepted_decision: two" in details


def test_two_accepted_tasks_agreeing_about_scope_are_not_a_conflict():
    """The control. Two tasks that cover one finding with the SAME scope agree,
    and a guard that reported them would fire on an ordinary registry."""
    accepted = TaskRegistry([
        Task(id="one", title="t", description="covers d1:f1",
             approved_paths=("a.py", "autoloop/tests/")),
        Task(id="two", title="t", description="covers d1:f1",
             approved_paths=("a.py", "autoloop/tests/")),
    ])
    proposal = generate_tasks(reconcile([finding("f1")]), accepted, tree=TREE)

    assert proposal.conflicts == []
    assert proposal.stopped == ""
    assert len(proposal.tasks) == 1


def test_a_longer_finding_id_is_not_a_mention_of_a_shorter_one():
    """`d1:f1` is not `d1:f11`. The substring test this replaces manufactured a
    conflict between two sources that were never talking about the same finding
    — and an invented conflict has to be disproved by hand before generation can
    run again, which is how a guard gets switched off."""
    accepted = TaskRegistry([
        Task(id="other", title="t", description="covers d1:f11", approved_paths=("z.py",)),
    ])
    proposal = generate_tasks(reconcile([finding("f1")]), accepted, tree=TREE)

    assert proposal.conflicts == []
    assert proposal.stopped == ""
    assert len(proposal.tasks) == 1

    # The real mention still lands, in every ordinary punctuation around it, or
    # the boundary rule would have closed the check instead of narrowing it.
    for description in ("covers d1:f1", "covers d1:f1.", "d1:f1: scoped", "(d1:f1)"):
        narrow = TaskRegistry([
            Task(id="one", title="t", description=description, approved_paths=("z.py",))
        ])
        assert generate_tasks(
            reconcile([finding("f1")]), narrow, tree=TREE
        ).stopped, description


def test_a_conflict_that_cannot_be_recorded_stops_even_when_scope_is_unchanged():
    """The fail-open this closes: "recorded, and generation continues" is only
    honest when the record EXISTS. Without a store there is nothing for an
    operator to list or answer, so the conflict is not waved through."""
    operator = Claim(
        text="the gate already returns False there",
        source=SOURCE_OPERATOR_REQUEST,
        author="the operator",
        subject="d1:f1",
        kind=CLAIM_BEHAVIOUR,
        citation=Evidence(text="the request", source="the operator's request"),
        paths=("a.py",),          # the SAME paths the finding names
    )
    proposal = generate_tasks(reconcile([finding("f1")]), registry(), sources=[operator])

    [conflict] = proposal.conflicts
    assert conflict.scope_impact == SCOPE_UNCHANGED
    assert conflict.stops_generation is False, "scope is genuinely unchanged"
    assert proposal.tasks == [], "and generation stopped anyway, for want of a record"
    assert "could not be recorded durably" in proposal.stopped
    # And the operator is told IN THE REPORT — `skipped` is the only field of a
    # proposal that `audit/report.py` renders. (The claim above is an operator
    # request offered as evidence of current behaviour, which precedence refuses,
    # so its own refusal is reported alongside: it is a party to the conflict and
    # is never dropped.)
    [(subject, reason)] = [
        entry for entry in proposal.skipped if entry[0] == "d1:f1"
    ]
    assert subject == "d1:f1"
    assert "NO DURABLE RECORD EXISTS" in reason
    assert any(
        "unsupported source claim" in entry[1] for entry in proposal.skipped
    ), "the uncited source was refused too, and still compared"


def test_an_accepted_task_with_no_scope_at_all_stops_rather_than_passes(tmp_path):
    """Pinned deliberately rather than left to be discovered: a task that names
    this finding and declares NO approved paths leaves the scope impact
    unmeasurable, and the tri-state's whole point is that unmeasurable takes the
    stopping branch. It is also, on its own terms, a task that cannot do what it
    was filed for."""
    store = BlockerStore(tmp_path / "blockers")
    accepted = TaskRegistry([Task(id="empty", title="t", description="covers d1:f1")])
    proposal = generate_tasks(
        reconcile([finding("f1")]), accepted, blocker_store=store, tree=TREE,
        now="2026-09-09T00:00:00Z",
    )

    assert proposal.tasks == []
    assert "scope_impact_unknown" in proposal.stopped
    assert "undispatchable" in proposal.stopped


def test_a_retired_task_is_not_an_accepted_constraint():
    """A withdrawn decision must not hold up generation forever."""
    accepted = TaskRegistry([
        Task(id="gone", title="t", description="covers d1:f1",
             approved_paths=("b.py",), status="retired"),
    ])
    proposal = generate_tasks(reconcile([finding("f1")]), accepted, tree=TREE)

    assert proposal.stopped == ""
    assert len(proposal.tasks) == 1


def test_a_finding_makes_no_conflict_with_its_own_sentences():
    """Every claim a finding makes shares its source. Comparing them would
    report each finding as self-contradictory and stop all generation."""
    rich = replace(
        cited("f1"),
        assumptions=("something else entirely",),
        open_questions=("and another thing",),
    )
    proposal = generate_tasks(reconcile([rich]), registry())

    assert proposal.conflicts == []
    assert len(proposal.tasks) == 1


def test_a_merged_finding_still_asserts_nothing_uncited():
    """`reconcile._merge` rebuilds a `Finding` field by field, so the ctx-06
    fields do not survive a fold (that file is outside ctx-06's scope). What
    must survive is the DIRECTION: a merged finding may lose a cited claim, and
    must never gain an uncited one."""
    a = replace(finding("a", files=("a.py",)), evidence="a.py:10 alpha")
    b = replace(
        finding("b", files=("a.py",)),
        evidence="a.py:44 beta",
        current_behaviour="uncited claim that must not reach a task",
        current_behaviour_citation="",
    )
    proposal = generate_tasks(reconcile([a, b]), registry())

    rendered = "\n".join(t.description for t in proposal.tasks)
    assert "uncited claim that must not reach a task" not in rendered


def test_a_blank_field_is_absent_rather_than_an_empty_heading():
    """The renderer has to judge blank exactly as `Finding.claims` does. Testing
    mere truthiness printed a "Current behaviour" heading with nothing under it
    and no citation — a line in the task that no refusal had ever looked at."""
    blank = replace(
        finding("f1"),
        current_behaviour="   ",
        current_behaviour_citation="",
        assumptions=("", "  "),
        open_questions=("",),
        context_ids=("",),
    )
    [task] = generate_tasks(reconcile([blank]), registry()).tasks

    for heading in (
        "Current behaviour",
        "Assumptions, stated rather than verified",
        "Unresolved questions",
        "Context records consulted",
    ):
        assert heading not in task.description, heading


def test_the_full_contract_reaches_the_description():
    """What a fresh session with no conversation history needs, in one place."""
    complete = replace(
        cited("f1"),
        context_ids=("ctx-42",),
        assumptions=("the migration already ran",),
        open_questions=("does anything else read this table?",),
    )
    [task] = generate_tasks(reconcile([complete]), registry()).tasks

    for fragment in (
        "Current behaviour, as the audit cited it:",
        "autoloop/policy.py:120",
        "Desired behaviour (scope):",
        "Context records consulted (REFERENCES, not evidence",
        "ctx-42",
        "Evidence the audit cited:",
        "Impact if unfixed, as the audit stated it:",
        "Assumptions, stated rather than verified:",
        "Unresolved questions:",
        "Non-goals:",
        "Expected files:",
        "Approved paths this task asks for, exactly:",
        "Acceptance criteria:",
        "Validation:",
    ):
        assert fragment in task.description, fragment


# ---- ctx-06 round 2: the citation is CHECKED, and the seam is wired ---------
#
# Round 1 required a citation to be path-SHAPED, which an agent gets for free by
# typing one. These pin the three things that closes: the location is checked
# against a tree, an unverifiable one is refused rather than believed, and the
# inputs that make any of it work in production travel on the object the
# executor actually passes.


def test_a_cited_location_the_tree_does_not_have_is_refused_and_named():
    """The hole: `evidence` naming a plausible file the tree does not have. The
    finding is refused, and the refusal says the path is not in the tree — not
    that the citation was missing, which would send an author to fix the wrong
    thing."""
    invented = replace(finding("f1"), evidence="autoloop/nowhere.py:12 saw it")
    proposal = generate_tasks(reconcile([invented]), registry())

    assert proposal.tasks == []
    [(finding_id, reason)] = proposal.skipped
    assert finding_id == "d1:f1"
    assert "not in the tree that was read" in reason
    assert "autoloop/nowhere.py" in reason


def test_a_citation_no_reader_could_check_is_refused_rather_than_believed():
    """The fail-open this closes: with no tree to ask, "we could not verify it"
    must not produce the same outcome as "we verified it"."""
    bare = TaskRegistry()          # no seam attached: no tree, no store
    proposal = generate_tasks(reconcile([finding("f1")]), bare)

    assert proposal.tasks == []
    assert "could not be verified against any tree" in proposal.skipped[0][1]


def test_read_nothing_and_found_nothing_are_different_refusals():
    """`repo_evidence`'s distinction, one level up. A checkout git could not
    answer for must not be reported as a checkout that lacks the file."""
    unread = TreeReader.of("/nonexistent-checkout-for-this-test")
    assert unread.read is False
    proposal = generate_tasks(reconcile([finding("f1")]), registry(), tree=unread)

    assert proposal.tasks == []
    reason = proposal.skipped[0][1]
    assert "nothing was read" in reason
    assert "NOTHING WAS READ" in reason
    assert "not in the tree" not in reason


def test_a_proposed_new_file_is_not_a_fabricated_citation():
    """The bound that keeps the check from refusing honest work: an INTENT claim
    says what should become true, so a finding proposing a file that does not
    exist yet cites a location that is correctly absent."""
    creating = replace(
        finding("f1"),
        proposed_action="add autoloop/brand_new.py and wire it in",
        impact="without autoloop/brand_new.py the gate has no home",
    )
    proposal = generate_tasks(reconcile([creating]), registry())

    assert [t.finding_ids for t in proposal.tasks] == [("d1:f1",)]
    assert proposal.skipped == []


def test_an_unsupported_source_is_recorded_compared_and_never_restated(tmp_path):
    """The other half of the review's finding: a `sources` claim bypassed the
    citation rule and was rendered into the task anyway. It must be REFUSED in
    the report, still compared (dropping it would hide the disagreement), still
    in the durable record verbatim — and absent from the description."""
    store = BlockerStore(tmp_path / "blockers")
    smuggled = Claim(
        text="autoloop/policy.py already returns False, so this is a no-op",
        source=SOURCE_CONTEXT_RECORD,
        author="ctx-99",
        subject="d1:f1",
        kind=CLAIM_BEHAVIOUR,
        citation=None,                      # nothing backs it at all
        paths=("a.py",),                    # the SAME scope, so it does not stop
    )
    proposal = generate_tasks(
        reconcile([finding("f1")]),
        registry(),
        sources=[smuggled],
        blocker_store=store,
        now="2026-09-09T00:00:00Z",
    )

    # Reported, by name, in the field the audit report renders.
    assert any(
        "unsupported source claim" in reason and "no-op" in reason
        for _, reason in proposal.skipped
    )
    # Still compared, and still durable, with the sentence intact for the
    # operator who has to settle it.
    [blocker] = store.open_blockers()
    assert "so this is a no-op" in blocker.detail
    # And NOT restated to the session that will execute the task.
    [task] = proposal.tasks
    assert "so this is a no-op" not in task.description
    assert "assertion WITHHELD" in task.description
    assert "ctx-99" in task.description, "the source is still named"


def test_a_supported_source_is_still_quoted_in_the_task(tmp_path):
    """The control for the test above, without which withholding could be
    implemented as 'never render a conflict' and still pass. A source that DID
    cite a reader keeps its words in the description."""
    store = BlockerStore(tmp_path / "blockers")
    supported = Claim(
        text="the gate already returns False there",
        source=SOURCE_REPOSITORY,
        author="a second reader",
        subject="d1:f1",
        kind=CLAIM_BEHAVIOUR,
        citation=Evidence(text="a.py:12", source="git show"),
        paths=("a.py",),
    )
    proposal = generate_tasks(
        reconcile([finding("f1")]),
        registry(),
        sources=[supported],
        blocker_store=store,
        now="2026-09-09T00:00:00Z",
    )

    [task] = proposal.tasks
    assert "the gate already returns False there" in task.description
    assert "assertion WITHHELD" not in task.description


def test_the_wired_seam_records_and_continues_without_the_executor_knowing(tmp_path):
    """THE PRODUCTION SHAPE. `audit/executor.py` calls
    `generate_tasks(reconciled, self._registry)` and passes nothing else, so this
    calls it exactly that way — with only the seam attached — and requires the
    acceptance criterion that needs a real store: a conflict that does not change
    scope is recorded ON DISK and generation carries on."""
    store = BlockerStore(tmp_path / "blockers")
    operator = Claim(
        text="the gate already returns False there",
        source=SOURCE_OPERATOR_REQUEST,
        author="the operator",
        subject="d1:f1",
        kind=CLAIM_BEHAVIOUR,
        citation=Evidence(text="the request", source="the operator's request"),
        paths=("a.py",),
    )
    wired = with_tree(
        TaskRegistry(),
        blocker_store=store,
        provider=lambda findings: ((operator,), ("a note about a tier",)),
    )

    proposal = generate_tasks(reconcile([finding("f1")]), wired)

    assert proposal.stopped == "", proposal.stopped
    assert len(proposal.tasks) == 1
    [blocker] = store.open_blockers()
    assert blocker.code == PLANNING_SOURCE_CONFLICT
    assert BlockerStore(tmp_path / "blockers").load(blocker.id) is not None, "durable"
    # The tier note reaches the rendered field, not just an object nobody prints.
    assert ("(planning sources)", "a note about a tier") in proposal.skipped


def test_an_explicit_sources_argument_beats_the_wired_provider():
    """A caller that states its sources is saying what the tiers are; a provider
    quietly adding more would make that statement untrue."""
    wired = with_tree(
        TaskRegistry(),
        provider=lambda findings: ((_scope_claim("b.py"),), ()),
    )
    proposal = generate_tasks(reconcile([finding("f1")]), wired, sources=[])

    assert proposal.conflicts == [], "the provider's claim was not consulted"
    assert len(proposal.tasks) == 1


def test_a_provider_that_raises_is_a_note_not_a_crash_and_not_silence():
    """A tier that could not be read is UNKNOWN, never agreement — and never an
    exception taking down a report that has findings to deliver."""

    def broken(findings):
        raise RuntimeError("the intake directory exploded")

    wired = with_tree(TaskRegistry(), provider=broken)
    proposal = generate_tasks(reconcile([finding("f1")]), wired)

    assert len(proposal.tasks) == 1
    assert any("NOTHING WAS READ" in reason for _, reason in proposal.skipped)
    assert any("the intake directory exploded" in note for note in proposal.record_notes)


def test_a_broken_seam_is_read_as_no_seam_rather_than_trusted():
    """Fail-closed on the seam itself: something that is not a `PlanningSources`
    must not be believed to be one."""
    junk = TaskRegistry()
    setattr(junk, PLANNING_SOURCES_ATTR, {"tree": TREE})   # a dict, not the type
    proposal = generate_tasks(reconcile([finding("f1")]), junk)

    assert proposal.tasks == [], "no tree was accepted from a malformed seam"
    assert "could not be verified against any tree" in proposal.skipped[0][1]


def test_the_executor_still_hands_the_generator_the_object_the_seam_is_on():
    """A drift guard, because the seam only works while `audit/executor.py`
    passes the registry `cli._build_executor` attached to. That file is outside
    this task's approved paths, so this asserts its call rather than changing
    it: if the call ever takes a registry from somewhere else, production
    silently loses the store, the tree and the operator's request at once."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "audit" / "executor.py").read_text(
        encoding="utf-8"
    )
    assert "generate_tasks(reconciled, self._registry)" in source
    assert "self._registry = registry" in source


def _scope_claim(path):
    return Claim(
        text=f"this work is scoped to {path}",
        source=SOURCE_OPERATOR_REQUEST,
        author="a draft",
        subject="d1:f1",
        kind=CLAIM_CONSTRAINT,
        citation=Evidence(text="draft.md → t.approved_paths", source="the draft file"),
        paths=(path,),
    )
