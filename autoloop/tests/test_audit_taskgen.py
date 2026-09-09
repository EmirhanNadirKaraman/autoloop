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
    SCOPE_UNCHANGED,
    SOURCE_OPERATOR_REQUEST,
    Claim,
    Evidence,
)
from autoloop.tasks import Task, TaskRegistry


def registry(*ids):
    return TaskRegistry([Task(id=i, title="t", description="d") for i in ids])


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
        reconcile([finding("f1")]), accepted, blocker_store=store, now="2026-09-09T00:00:00Z"
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
    proposal = generate_tasks(reconcile([finding("f1")]), accepted)

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
    proposal = generate_tasks(reconcile([finding("f1")]), accepted, blocker_store=store)

    assert proposal.tasks == []
    assert proposal.stopped
    assert any("could NOT be recorded durably" in note for note in proposal.record_notes)


def test_no_store_at_all_still_stops_and_says_nothing_was_recorded():
    accepted = TaskRegistry([
        Task(id="already", title="t", description="covers d1:f1", approved_paths=("b.py",))
    ])
    proposal = generate_tasks(reconcile([finding("f1")]), accepted)

    assert proposal.tasks == []
    assert proposal.stopped
    assert any("NOT recorded durably" in note for note in proposal.record_notes)


def test_a_retired_task_is_not_an_accepted_constraint():
    """A withdrawn decision must not hold up generation forever."""
    accepted = TaskRegistry([
        Task(id="gone", title="t", description="covers d1:f1",
             approved_paths=("b.py",), status="retired"),
    ])
    proposal = generate_tasks(reconcile([finding("f1")]), accepted)

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
        "Assumptions, stated rather than verified:",
        "Unresolved questions:",
        "Non-goals:",
        "Expected files:",
        "Approved paths this task asks for, exactly:",
        "Acceptance criteria:",
        "Validation:",
    ):
        assert fragment in task.description, fragment
