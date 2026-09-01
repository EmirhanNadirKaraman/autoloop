"""Refused work goes back to the agent as a `revise`, and steps aside if the
same refusal repeats (halt-04, 2026-09-01).

The one claim under test, stated as the loop has to satisfy it:

    With `autonomy.enabled` on, `post_commit_verification_failed`,
    `commit_refused`, `review_feedback_unchanged`, `review_packet_build_failed`,
    `approved_paths_missing`, `push_not_descendant` and `push_tree_mismatch` are
    each sent back as a `revise` carrying the refusal text as feedback — and when
    the SAME refusal repeats, the task is set aside instead of looping. With the
    flag off, every one of them parks exactly as it did before. The five hard
    halts are unreachable from any of it.

**THE REPEAT GUARD IS THE HARD PART, not the resend**, so most of this file is
about the bound rather than about the send. Sections 4 and 5 own it, and section
5 exists for one specific fail-open that an earlier design would have shipped: an
autonomous retry marks its blocker for closure by the next COMPLETED step, and a
revise round completing IS such a step — so closing the record there would tell
an operator the loop recovered from a refusal that is still standing.
`test_a_completed_revise_round_does_not_refund_the_allowance` is the regression,
and it also pins the property that makes the meter survive it: the allowance is
counted across CLOSED records too, so no closure can refund one.

The meter is keyed on the REFUSAL, not on its code
(`BlockerStore.refusal_revises` over `blockers.refusal_identity`), and both
directions are asserted because each is half the claim. The same complaint twice
sets the task aside — that is the loop this task exists to prevent. A DIFFERENT
complaint under the same code gets its own revise — `post_commit_verification_
failed` names five different checks, and refusing the second on the strength of
the first would park work that had an answer. What the identity key deliberately
does NOT bound is a code whose text churns every round; that is bounded by
`MAX_TASK_ATTEMPTS` and `policy.max_review_rounds`, and section 4 pins the way
that chain terminates.

Self-contained per this codebase's convention (see `test_blockers.py`'s
docstring) — the small config/orchestrator helpers are duplicated here rather
than imported from another test module.
"""

from __future__ import annotations

import json

import pytest

from autoloop import cli
from autoloop.blockers import (
    AUTONOMOUS_RECOVERIES,
    HARD_HALT_CODES,
    NO_TASK,
    RECOVER_BY_REVISING,
    RECOVER_UNAVAILABLE,
    REFUSED_WORK_RECOVERIES,
    BlockerStore,
    autonomous_recovery,
    refusal_identity,
)
from autoloop.errors import StateCorruptError
from autoloop.config import AutoloopConfig, AutonomyConfig, BrowserConfig
from autoloop.contract import Decision
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger

URL = "https://chatgpt.com/c/refusal-revise-test"

#: The seven codes halt-04 names, with the `kind` their own park site passes —
#: which is NOT uniform, and asserting one kind for all seven would be a claim
#: about the sites rather than about this change. The five produce-then-review
#: refusals classify `task_fatal`; the two push refusals classify `loop_fatal`,
#: and their conversion to a set-aside is part of what is under test.
SITE_KIND = {
    "post_commit_verification_failed": "task_fatal",
    "commit_refused": "task_fatal",
    "review_feedback_unchanged": "task_fatal",
    "review_packet_build_failed": "task_fatal",
    "approved_paths_missing": "task_fatal",
    "push_not_descendant": "loop_fatal",
    "push_tree_mismatch": "loop_fatal",
}

SPECIFIED_CODES = tuple(SITE_KIND)

#: A refusal that reads like a real one: the park's own prose, and a `detail`
#: the site would have passed separately.
REFUSAL = (
    "task t1: commit 0123456789ab on autoloop/t1 (round 1) was created but "
    "REFUSED at post-commit review. Reasons: post-commit validation failed"
)
DETAIL = "post-commit validation failed: 1 test failed"


# =============================================================================
# helpers
# =============================================================================


def make_config(tmp_path, *, enabled=True, max_recovery_attempts=2) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers_root",
        autonomy=AutonomyConfig(
            enabled=enabled, max_recovery_attempts=max_recovery_attempts
        ),
    )


def build(tmp_path, *, enabled=True, max_recovery_attempts=2, with_store=True,
          tasks=("t1",), in_flight="t1", dispatched=None,
          decomposition="approach: fix it; files: a.py; steps: one"):
    """A collaborator-free Orchestrator, exactly as `test_autonomous_recovery.
    build` makes one — `_to_needs_user` and everything halt-04 adds to it touch
    only `state`, `_log`, `_blocker_store`, `_store`, `_registry` and `_policy`.

    Every task carries a `decomposition`, because that is the state a task is
    genuinely in when one of these refusals is raised: a refusal happens after a
    round has been dispatched, and no round is dispatched without an approved
    plan (`policy._check_decomposition`). A task without one is a real case and
    has its own test in section 6, where the policy gate refuses the revise.
    """
    config = make_config(
        tmp_path, enabled=enabled, max_recovery_attempts=max_recovery_attempts
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    if in_flight is not None:
        state.task_execution = {"task_id": in_flight}
    if dispatched is not None:
        state.current_task = {"task_id": dispatched, "title": f"Title {dispatched}"}
    store.save(state)
    registry = TaskRegistry([
        Task(id=tid, title=f"Title {tid}", description="d", approved_paths=("a.py",),
             decomposition=decomposition)
        for tid in tasks
    ])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    blocker_store = BlockerStore(config.blockers_dir) if with_store else None
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=None,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        blocker_store=blocker_store,
    )
    return orch, config, blocker_store, task_store, registry


def capture_dispatch(orch) -> list:
    """Replace `_dispatch` with a recorder and hand back the list it fills.

    The claim is about WHICH directive the loop issues and how often, not about
    what an executor then does with it, so the round itself is deliberately not
    run: a real one costs a worker repository, an agent and a validation
    subprocess to re-prove machinery `test_postcommit_flow.py` already owns.
    `test_a_self_issued_revise_reaches_the_executor_branch` is the one test that
    keeps this substitution honest, by driving the REAL `_dispatch`.
    """
    seen: list = []
    orch._dispatch = seen.append  # type: ignore[method-assign]
    return seen


def refuse(orch, code, *, question=REFUSAL, detail=DETAIL, task_id="t1",
           phase=Phase.EXECUTING.value):
    """One occurrence of `code`, raised the way its own site raises it."""
    orch.state.phase = phase
    orch._to_needs_user(
        question, kind=SITE_KIND[code], code=code, task_id=task_id, detail=detail
    )


def transcript_types(config) -> list[str]:
    if not config.transcript_file.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def transcript_entries(config, entry_type: str) -> list[dict]:
    if not config.transcript_file.exists():
        return []
    out = []
    for line in config.transcript_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == entry_type:
            out.append(entry.get("data") or {})
    return out


def emitted_blocker_codes() -> set[str]:
    """Every string literal reachable as a `code=` argument of a blocker-emitting
    call in `orchestrator.py`. A local copy of
    `test_autonomous_recovery.emitted_blocker_codes`, for the reason that one
    gives: a reachability check that depends on another test module staying
    importable can be switched off by an unrelated edit."""
    import ast
    import inspect

    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("_to_needs_user", "_to_fault_stop"):
            continue
        for kw in node.keywords:
            if kw.arg != "code":
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    codes.add(sub.value)
    return codes


# =============================================================================
# 1. the table — all seven, one action, one allowance
# =============================================================================


def test_the_table_holds_exactly_the_seven_specified_codes():
    assert set(REFUSED_WORK_RECOVERIES) == set(SPECIFIED_CODES)
    assert len(REFUSED_WORK_RECOVERIES) == 7
    for code, entry in REFUSED_WORK_RECOVERIES.items():
        assert entry.code == code, "the table is keyed by its own code"
        assert entry.action == RECOVER_BY_REVISING
        assert entry.stale_record == "", "a revise archives nothing"
        assert entry.why.strip(), f"{code} automates without saying why"


def test_every_entry_allows_exactly_one_revise():
    """THE bound, read straight off the table. One is not a tuning constant
    here: two would mean a refusal could be resent after a resend already
    failed, which is the churn `review_feedback_unchanged` exists to name."""
    for code, entry in REFUSED_WORK_RECOVERIES.items():
        assert entry.max_attempts == 1, f"{code} allows more than one revise"


def test_the_seven_are_merged_into_the_one_lookup():
    assert set(REFUSED_WORK_RECOVERIES) <= set(AUTONOMOUS_RECOVERIES)
    for code, entry in REFUSED_WORK_RECOVERIES.items():
        assert autonomous_recovery(code) is entry


def test_every_refused_work_code_is_one_the_orchestrator_can_actually_raise():
    """The rule halt-02 stated and this inherits: a code no live site can raise
    should be REMOVED, not automated."""
    unreachable = set(REFUSED_WORK_RECOVERIES) - emitted_blocker_codes()
    assert not unreachable, f"automated codes nothing emits: {sorted(unreachable)}"


def test_the_five_hard_halts_stay_disjoint_and_refused_after_the_table_grew():
    """halt-02's guarantee, re-asserted against the MERGED table: adding seven
    entries must not have brought a hard halt in with them."""
    assert HARD_HALT_CODES.isdisjoint(set(AUTONOMOUS_RECOVERIES))
    assert HARD_HALT_CODES == {
        "checkout_escape_detected",
        "worker_isolation_violation",
        "primary_checkout_dirty",
        "approved_path_symlink_traversal",
        "prompt_integrity_mismatch",
    }
    for code in HARD_HALT_CODES:
        assert autonomous_recovery(code) is None


# =============================================================================
# 2. DEFAULT OFF — the reversibility half of the claim
# =============================================================================


@pytest.mark.parametrize("code", SPECIFIED_CODES)
def test_with_the_flag_off_every_named_code_parks_exactly_as_before(tmp_path, code):
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=False)

    refuse(orch, code)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == SITE_KIND[code], "the off position re-classified a park"
    assert orch.state.park_task_id == "t1"
    assert orch.state.question == REFUSAL
    assert orch._pending_autonomous_revise is None
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.code, blocker.task_id) == (SITE_KIND[code], code, "t1")
    assert blocker.revised_refusals == [], (
        "the off position spent a revise allowance without issuing a revise"
    )
    assert "autonomous_recovery" not in transcript_types(config)


def test_with_the_flag_off_a_repeat_still_parks_rather_than_setting_aside(tmp_path):
    """The other half of reversibility: the repeat guard is not a behaviour the
    off position inherits either. Two identical refusals, two parks."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=False)
    for _ in range(2):
        refuse(orch, "post_commit_verification_failed")
        assert orch.state.phase == Phase.NEEDS_USER.value
        assert orch.state.park_kind == "task_fatal"
    assert blocker_store.open_recurrences("t1", "post_commit_verification_failed") == 2


# =============================================================================
# 3. stage 1 — the refusal goes back as a revise, verbatim
# =============================================================================


@pytest.mark.parametrize("code", SPECIFIED_CODES)
def test_each_code_is_returned_as_a_revise_carrying_the_refusal_text(tmp_path, code):
    """THE claim's first half, once per code.

    `REFUSAL` must appear VERBATIM: the whole point is that the loop returns what
    an operator would have been shown, not a summary of it, and a paraphrase
    would be the loop editing its own evidence."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)

    refuse(orch, code)

    # It did NOT park.
    assert orch.state.phase == Phase.READY.value
    assert orch.state.question is None
    assert orch.state.park_kind is None
    assert orch.state.park_blocker_id is None
    assert "needs_user" not in transcript_types(config)
    # A revise is owed, against the task whose round is in flight, and it is
    # queued rather than dispatched from inside the park handler.
    assert dispatched == [], "the revise was dispatched from inside `_to_needs_user`"
    task_id, feedback, queued_code = orch._pending_autonomous_revise
    assert (task_id, queued_code) == ("t1", code)
    assert REFUSAL in feedback, "the refusal text was not returned verbatim"
    assert DETAIL in feedback, "the site's `detail` was dropped"
    assert code in feedback, "the feedback does not name the refusal it carries"
    # And the fault is on the record, OPEN, with the repeat guard's identity.
    open_records = blocker_store.open_blockers()
    assert [(b.code, b.task_id, b.recurrences) for b in open_records] == [(code, "t1", 1)]
    assert open_records[0].revised_refusals == [refusal_identity(code, REFUSAL, DETAIL)], (
        "the allowance was not spent against this refusal's own identity"
    )
    assert blocker_store.refusal_revises("t1", refusal_identity(code, REFUSAL, DETAIL)) == 1
    recovery = transcript_entries(config, "autonomous_recovery")
    assert recovery and recovery[0]["action"] == RECOVER_BY_REVISING
    assert (recovery[0]["attempt"], recovery[0]["budget"]) == (1, 1)


def test_the_feedback_says_no_reviewer_wrote_it(tmp_path):
    """An ECHO guard. The `feedback` field of a `revise` is ordinarily the
    REVIEWER's words, and it reaches the agent's prompt and
    `execution.last_revise_feedback`. A self-issued one that read like a
    reviewer's would come back later as if a reviewer had judged the work, so it
    is labelled at the top."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    capture_dispatch(orch)

    refuse(orch, "commit_refused")

    _, feedback, _ = orch._pending_autonomous_revise
    assert feedback.startswith("THE LOOP REFUSED YOUR LAST ROUND.")
    assert "no reviewer wrote it" in feedback
    assert feedback.index("THE LOOP REFUSED") < feedback.index(REFUSAL)


def test_the_queued_revise_is_dispatched_at_the_next_step_boundary(tmp_path):
    """`_step` performs it, so it sits inside `run`'s try with every transport,
    git and state handler an ordinary step has — and consumes one step, because
    it runs a real round."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)
    refuse(orch, "post_commit_verification_failed")

    orch._step(Phase(orch.state.phase))

    assert len(dispatched) == 1
    directive = dispatched[0]
    assert directive.decision is Decision.REVISE
    assert directive.task_id == "t1"
    assert REFUSAL in directive.feedback
    assert "post_commit_verification_failed" in directive.reason
    assert orch._pending_autonomous_revise is None, "the queue was not cleared"
    assert transcript_entries(config, "autonomous_revise_dispatched")[0]["task_id"] == "t1"


def test_the_queue_is_cleared_before_the_dispatch_not_after(tmp_path):
    """A dispatch that RAISES must leave nothing behind for the next step to run
    a second time — the blocker is already open with the recurrence counted, so
    clearing first loses no evidence and costs at most the park the loop would
    have performed anyway."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    refuse(orch, "commit_refused")

    def exploding_dispatch(directive):
        raise RuntimeError("the round died")

    orch._dispatch = exploding_dispatch  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        orch._step(Phase(orch.state.phase))

    assert orch._pending_autonomous_revise is None


def test_a_self_issued_revise_reaches_the_executor_branch(tmp_path):
    """The test that keeps `capture_dispatch` honest: the REAL `_dispatch`, and
    a `revise` falls through to `_dispatch_executor` exactly as a reviewer's
    does — no push binding is resolved for it and no retired-decision branch
    catches it."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    seen: list = []
    orch._dispatch_executor = seen.append  # type: ignore[method-assign]
    refuse(orch, "review_packet_build_failed")

    orch._step(Phase(orch.state.phase))

    assert len(seen) == 1
    assert seen[0].decision is Decision.REVISE and seen[0].task_id == "t1"


def test_the_state_file_agrees_with_the_object(tmp_path):
    """A crash here resumes at the round boundary, not in the phase the refusal
    was raised in — and the in-memory revise is lost, which leaves the blocker
    open with its allowance spent. Fewer revises, never more."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    capture_dispatch(orch)

    refuse(orch, "push_tree_mismatch")

    reloaded = StateStore(config.state_file).load()
    assert reloaded.phase == Phase.READY.value
    assert reloaded.park_kind is None and reloaded.question is None
    assert blocker_store.open_recurrences("t1", "push_tree_mismatch") == 1


# =============================================================================
# 4. stage 2 — the SAME refusal sets the task aside
# =============================================================================


@pytest.mark.parametrize("code", SPECIFIED_CODES)
def test_the_same_refusal_repeating_sets_the_task_aside(tmp_path, code):
    """THE claim's second half, once per code: one revise, then a quarantine —
    never a second revise."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)

    refuse(orch, code)
    assert orch._pending_autonomous_revise is not None
    orch._step(Phase(orch.state.phase))  # the revise round runs
    assert len(dispatched) == 1

    refuse(orch, code)  # the identical refusal comes back

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert orch._pending_autonomous_revise is None, "a second revise was issued"
    assert len(dispatched) == 1, "a second revise was dispatched"
    assert transcript_types(config).count("autonomous_recovery") == 1
    refusals = transcript_entries(config, "autonomous_revise_refused")
    assert refusals and refusals[-1]["reason"] == "same_refusal_repeated", (
        "the guard fired without saying it was the same refusal"
    )


def test_a_DIFFERENT_refusal_for_the_same_code_gets_its_own_revise(tmp_path):
    """THE reason the meter is keyed on the refusal rather than on its code.

    `post_commit_verification_failed` is raised for five different checks and
    `commit_refused` for every reason git declined one. A second occurrence
    saying something genuinely different is feedback the agent has never been
    given, and a (task, code) meter would refuse it on the strength of a fault it
    has nothing to do with — parking work that had an answer. Here the second
    refusal is a different complaint, and it is owed its own revise."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)

    refuse(orch, "post_commit_verification_failed",
           question="task t1: commit aaaaaaaaaaaa (round 1) was REFUSED",
           detail="worktree is not clean after commit")
    orch._step(Phase(orch.state.phase))

    refuse(orch, "post_commit_verification_failed",
           question="task t1: commit bbbbbbbbbbbb (round 2) was REFUSED",
           detail="post-commit validation failed: 3 tests failed")
    orch._step(Phase(orch.state.phase))

    assert len(dispatched) == 2, "a genuinely new refusal was never returned"
    assert "post-commit validation failed: 3 tests failed" in dispatched[1].feedback
    assert orch.state.phase == Phase.READY.value, "the second complaint parked"
    assert not transcript_entries(config, "autonomous_revise_refused")
    # Two identities, one allowance each, both spent — and neither refunds the
    # other.
    spent = [b.revised_refusals for b in blocker_store.all_blockers()]
    assert sum(len(entries) for entries in spent) == 2
    assert len({fp for entries in spent for fp in entries}) == 2


def test_the_third_occurrence_of_the_FIRST_refusal_is_still_set_aside(tmp_path):
    """The other half of the test above, and the one that stops it being a hole.

    Letting a different complaint through must not refund the first one's
    allowance: the meter is per identity, so refusal A, refusal B, refusal A
    again is one revise for A, one for B, and a set-aside for A's return — never
    a rotation that revises forever."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)
    first = dict(question="task t1: commit aaaaaaaaaaaa was REFUSED", detail="dirty")
    second = dict(question="task t1: commit bbbbbbbbbbbb was REFUSED", detail="ancestry")

    for refusal in (first, second, first):
        refuse(orch, "post_commit_verification_failed", **refusal)
        if orch._pending_autonomous_revise is not None:
            orch._step(Phase(orch.state.phase))

    assert len(dispatched) == 2, "the first refusal came back and bought a revise"
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert transcript_entries(config, "autonomous_revise_refused")[-1]["reason"] == (
        "same_refusal_repeated"
    )


def test_the_allowance_cannot_be_refreshed_by_the_refusal_moving_one_phase_along(
    tmp_path,
):
    """`BlockerStore.record` keys on phase — correctly, for an operator question
    — while `refusal_identity` does not, so the SAME refusal raised in `executing`
    and again in `ready` (which is exactly where a self-issued revise leaves the
    loop) is one allowance across two records, not two allowances."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)

    refuse(orch, "commit_refused", phase=Phase.EXECUTING.value)
    orch._step(Phase(orch.state.phase))  # the revise round; nothing is queued now

    refuse(orch, "commit_refused", phase=Phase.READY.value)

    assert len(dispatched) == 1, "a phase change bought a second revise"
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert len(blocker_store.open_blockers()) == 2, "two records, one budget"
    assert transcript_entries(config, "autonomous_revise_refused")[-1]["reason"] == (
        "same_refusal_repeated"
    ), "the phase-blind identity meter did not recognise the refusal"


def test_the_set_aside_park_makes_continuous_mode_continue(tmp_path, capsys):
    """The half that matters: `cli._handle_parked_task` — unmodified by halt-04
    — quarantines the one task and tells the caller to carry on, so a task whose
    refusal will not clear costs the roadmap one task rather than the run."""
    orch, config, _, task_store, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2")
    )
    capture_dispatch(orch)
    refuse(orch, "push_not_descendant")
    orch._step(Phase(orch.state.phase))
    refuse(orch, "push_not_descendant")

    verdict = cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )

    assert verdict == "task_fatal"
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert reloaded.state_of("t2") is TaskState.READY  # the loop still has work
    assert "continuous mode continues" in capsys.readouterr().out


def test_a_repeat_after_the_record_was_closed_is_still_recognised(tmp_path):
    """WHY the identity lives on the blocker record and is read across CLOSED
    ones too.

    An operator answering the blocker, `archive_stale` retiring its session, or
    `close_recovered` closing it after some other retry all end the episode — and
    a guard that forgot the refusal at that moment would hand the identical
    refusal a fresh allowance the moment the record went away. Here the record is
    closed between the two occurrences and the second is still recognised."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)

    refuse(orch, "review_feedback_unchanged")
    orch._step(Phase(orch.state.phase))
    blocker_store.resolve(blocker_store.open_blockers()[0].id, "have a look")

    refuse(orch, "review_feedback_unchanged")

    assert len(dispatched) == 1, "a closed record bought a second revise"
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")


def test_refusal_identity_is_stable_normalised_and_empty_for_nothing():
    """The digest's own contract. Whitespace and case must not read as a new
    refusal (the same rule `_normalise_feedback` states), two different
    complaints always must, and NO text has NO identity — `""` may never read as
    "these two match", or two absences would set a task aside."""
    base = refusal_identity("commit_refused", "  The COMMIT   was refused ", "d")
    assert base == refusal_identity("commit_refused", "the commit was refused", "d")
    assert base != refusal_identity("commit_refused", "the commit was refused", "e")
    assert base != refusal_identity("push_tree_mismatch", "the commit was refused", "d")
    assert refusal_identity("commit_refused", "", "") == ""
    assert refusal_identity("commit_refused", "   ", "\n") == ""
    assert refusal_identity("", "", "") == ""


def test_a_stored_empty_identity_never_matches_a_textless_refusal(tmp_path):
    """The direction the empty digest must fail in.

    The store already holds a CLOSED record for this (task, code) that spent no
    allowance — a record written before the field existed, or by a park raised
    while the flag was off. A textless refusal arrives on top of it, and the two
    absences must not read as a match: that would set a task aside on a
    coincidence rather than on a repeat. It is refused as `empty_refusal_text`
    instead, which is the honest reason — ONCE, because the guard returns before
    `_queue_autonomous_revise` can log the same fact a second time."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    seeded = blocker_store.record(
        task_id="t1", kind="task_fatal", code="commit_refused", question="",
        detail="", phase="ready", now="2026-09-01T00:00:00+00:00",
    )
    blocker_store.resolve(seeded.id, "answered by hand")
    assert seeded.revised_refusals == []
    assert blocker_store.refusal_revises("t1", "") == 0, (
        "an empty identity counted a spent allowance"
    )

    refuse(orch, "commit_refused", question="", detail="")

    reasons = [e["reason"] for e in transcript_entries(config, "autonomous_revise_refused")]
    assert reasons == ["empty_refusal_text"], (
        f"an absent identity matched an absent identity: {reasons}"
    )
    assert orch.state.phase == Phase.NEEDS_USER.value


def test_an_empty_identity_can_never_be_written_into_the_meter(tmp_path):
    """The store's own half of the rule above. `""` stored once would match every
    later textless refusal, so the meter refuses to hold it at all rather than
    trusting its caller to have checked."""
    from autoloop.errors import StateError

    _, _, blocker_store, _, _ = build(tmp_path, enabled=True)
    blocker = blocker_store.record(
        task_id="t1", kind="task_fatal", code="commit_refused", question="q",
        detail="d", phase="ready", now="2026-09-01T00:00:00+00:00",
    )

    with pytest.raises(StateError):
        blocker_store.note_refusal_revise(blocker.id, "")

    assert blocker_store.load(blocker.id).revised_refusals == []


# =============================================================================
# 5. THE FAIL-OPEN: a completed round must not refund the allowance
# =============================================================================
#
# `_autonomous_retry` marks its blocker for closure by the first COMPLETED step,
# because for a transport fault a step completing IS evidence the fault passed.
# For a revise it is evidence of nothing of the kind — the round ran, that is
# all, and the refusal is usually raised BY that round. Closing the record there
# would file a machine `archived_reason` claiming the loop recovered from a
# refusal still standing. The meter itself is counted across CLOSED records too,
# so no closure — by this marker, by an operator's answer, or by `archive_stale`
# — can refund an allowance. Both properties are asserted, because the marker
# being wrong and the meter being refundable are two different defects.


def test_a_completed_revise_round_does_not_refund_the_allowance(tmp_path):
    """THE regression, driven the way `run` drives it: a step, then `run`'s own
    `else` branch (`_close_recovered_blocker`), then the same refusal again.

    Asserted in four places rather than one, because each catches a different
    way of getting this wrong: the marker is never set, the record is still open
    after a completed step, the spent allowance is still on the record, and the
    second refusal parks."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)
    identity = refusal_identity("post_commit_verification_failed", REFUSAL, DETAIL)

    refuse(orch, "post_commit_verification_failed")
    assert orch._autonomous_recovered_blocker == "", (
        "the revise armed the close-on-next-completed-step marker"
    )

    orch._step(Phase(orch.state.phase))       # the revise round
    orch._close_recovered_blocker()           # `run`'s else branch, verbatim

    assert len(blocker_store.open_blockers()) == 1, "the completed round closed the record"
    assert blocker_store.refusal_revises("t1", identity) == 1

    refuse(orch, "post_commit_verification_failed")

    assert len(dispatched) == 1, "the refund handed the same refusal a second revise"
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")


def test_answering_the_blocker_does_not_refund_the_allowance_either(tmp_path):
    """The same property against the OTHER two ways a record closes. An operator
    answering it and `archive_stale` retiring its session both end the episode,
    and a meter read off open records alone would hand the identical refusal a
    fresh allowance at that moment."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)
    identity = refusal_identity("commit_refused", REFUSAL, DETAIL)

    refuse(orch, "commit_refused")
    orch._step(Phase(orch.state.phase))
    blocker_store.archive_stale(blocker_store.open_blockers()[0].id, "session retired")

    assert blocker_store.open_blockers() == []
    assert blocker_store.refusal_revises("t1", identity) == 1, (
        "closing the record refunded the allowance"
    )

    refuse(orch, "commit_refused")

    assert len(dispatched) == 1
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")


def test_a_transport_retry_still_closes_its_record(tmp_path):
    """The contrast that makes the test above mean something: the carve-out is
    for `RECOVER_BY_REVISING` alone, and halt-02's marker still arms for a
    resume."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.AWAITING.value
    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")
    assert orch._autonomous_recovered_blocker

    orch._close_recovered_blocker()

    assert blocker_store.open_blockers() == []


def test_ten_consecutive_identical_refusals_produce_exactly_one_revise(tmp_path):
    """The bound stated as a number rather than as a shape. Whichever phase it is
    raised in and however many records it spreads across, ONE refusal buys ONE
    self-issued revise — the other nine park."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)

    for round_number in range(10):
        refuse(
            orch,
            "review_packet_build_failed",
            question="task t1: the round could not be presented",
            phase=(Phase.EXECUTING.value if round_number % 2 else Phase.READY.value),
        )
        if orch._pending_autonomous_revise is not None:
            orch._step(Phase(orch.state.phase))
        orch._close_recovered_blocker()

    assert len(dispatched) == 1
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"


# =============================================================================
# 5b. what bounds a code whose TEXT churns, which the identity meter does not
# =============================================================================
#
# The identity key is deliberately exact, so a refusal that says something new
# every round is not stopped HERE. It is stopped by the ceilings a self-issued
# revise spends exactly as a reviewer's does, and then by the quarantine those
# ceilings produce. Both ends of that chain are pinned below rather than
# asserted in prose, and neither costs a real round: `test_a_self_issued_revise_
# reaches_the_executor_branch` above already proves the revise arrives at
# `_dispatch_executor`, which is the site that charges `MAX_TASK_ATTEMPTS`.


def test_the_ceilings_that_bound_a_churning_refusal_are_themselves_set_asides():
    """`attempt_count_ceiling` and `review_round_cap` terminate the chain, and
    they terminate it in a QUARANTINE rather than in another revise: both are
    `RECOVER_UNAVAILABLE` with a budget of 0, so the set-aside fires on their
    first occurrence. Neither is in halt-04's table, so neither can be answered
    with the revise that spent them."""
    for code in ("attempt_count_ceiling", "review_round_cap"):
        entry = autonomous_recovery(code)
        assert entry is not None, f"{code} is not automated at all"
        assert entry.action == RECOVER_UNAVAILABLE, f"{code} retries instead of stopping"
        assert entry.max_attempts == 0
        assert code not in REFUSED_WORK_RECOVERIES


def test_a_task_already_set_aside_cannot_be_revised_back_out_of_quarantine(tmp_path):
    """The far end of the chain. A set-aside leaves the task
    `blocked_by_operator`, and `policy.authorize_directive` refuses a `revise` of
    one — so once the ceilings quarantine a task, no later refusal can start
    another round on it, whatever its text says."""
    orch, config, _, task_store, registry = build(tmp_path, enabled=True)
    registry.block("t1", "quarantined by an earlier ceiling")
    task_store.save(registry)

    refuse(orch, "post_commit_verification_failed")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch._pending_autonomous_revise is None
    assert transcript_entries(config, "autonomous_revise_refused")[0]["reason"] == (
        "policy_denied:task_blocked_by_operator"
    )


# =============================================================================
# 6. fail-closed edges — every refusal parks with the question it always had
# =============================================================================


def test_a_refusal_naming_no_task_parks_exactly_as_today(tmp_path):
    """The changeset arms of `push_not_descendant` / `push_tree_mismatch`: an
    operator's changeset has no roadmap task, so there is nothing to revise and
    nothing to set aside. The site's own `loop_fatal` terminal stands.

    Refused ONE LEVEL UP, in `_to_needs_user`'s existing set-aside gate rather
    than in `_queue_autonomous_revise`: every plan here needs a task, so a park
    with none drops the plan before the queue is reached. That is why no
    `autonomous_revise_refused` entry is expected here — the transcript records
    this the way it already recorded it, and the `no_task_to_revise` branch in
    the queue is the defensive floor under a caller that does not exist yet."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("changeset push refused — candidate is not a descendant",
                        kind="loop_fatal", code="push_not_descendant")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id is None
    assert orch._pending_autonomous_revise is None
    assert blocker_store.load(orch.state.park_blocker_id).task_id == NO_TASK
    assert "autonomous_recovery" not in transcript_types(config)


def test_a_refusal_naming_a_task_that_is_not_the_round_in_flight_is_refused(tmp_path):
    """setaside-01's guard, inherited unchanged: `_dispatch_task_push` names
    `binding.task_id`, which can be a task other than the one executing. A
    bystander is neither revised nor quarantined, and the site's terminal
    survives."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, tasks=("t1", "t2"),
                                  in_flight="t1")

    refuse(orch, "push_tree_mismatch", task_id="t2")

    assert orch.state.park_kind == "loop_fatal"
    assert orch._pending_autonomous_revise is None
    refusals = transcript_entries(config, "autonomous_set_aside_refused")
    assert refusals and refusals[0]["reason"] == "named_task_is_not_the_active_task"


def test_a_refusal_with_no_text_is_refused_rather_than_dispatched_empty(tmp_path):
    """A `revise` with nothing to say cannot converge, so it is not sent."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)

    refuse(orch, "commit_refused", question="   ", detail="")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch._pending_autonomous_revise is None
    assert transcript_entries(config, "autonomous_revise_refused")[0]["reason"] == (
        "empty_refusal_text"
    )


def test_a_task_with_no_approved_decomposition_is_refused_by_policy(tmp_path):
    """The same gate a reviewer's `revise` passes, asked here rather than
    trusted: `policy._check_decomposition` refuses a task with no plan on
    record, so the loop cannot start one by revising it."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, decomposition="")

    refuse(orch, "post_commit_verification_failed")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch._pending_autonomous_revise is None
    reason = transcript_entries(config, "autonomous_revise_refused")[0]["reason"]
    assert reason == "policy_denied:decomposition_missing"


def test_a_task_the_registry_does_not_hold_is_refused(tmp_path):
    """`_resolve_audit_task`'s synthetic units are the live shape: an audit unit
    id is not a registry task, so a refusal naming one revises nothing."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, tasks=("t1",),
                                  in_flight="audit-0007")

    refuse(orch, "review_packet_build_failed", task_id="audit-0007")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch._pending_autonomous_revise is None
    assert transcript_entries(config, "autonomous_revise_refused")[0]["reason"] == (
        "task_not_in_registry"
    )


def test_an_outstanding_stat_only_split_ask_refuses_the_revise(tmp_path):
    """A question this loop has ALREADY asked, and must not answer itself.

    The reviewer was shown a stat and no patch and asked for a split plan
    (split-05); only a `split` naming that task may proceed, and every other
    reply parks on `review_packet_build_failed`. A self-issued revise IS one of
    those replies — so queueing it would spend that code's one allowance on a
    park about a split nobody re-asked for, and the operator's blocker would say
    the reviewer declined a split it was never shown. Refused at queue time
    instead, on a read-only predicate: nothing is re-prompted and no denial
    budget is spent."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch._stat_only_split_review_task = lambda: "t1"  # type: ignore[method-assign]

    refuse(orch, "post_commit_verification_failed")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch._pending_autonomous_revise is None
    assert transcript_entries(config, "autonomous_revise_refused")[0]["reason"] == (
        "stat_only_split_ask_outstanding"
    )


def test_a_live_urgent_pin_naming_another_task_refuses_the_revise(tmp_path):
    """The operator has said which unit of work comes next. "A refusal is
    feedback" is not a licence to jump that queue, and letting the revise reach
    `_dispatch_executor` would answer it through `_handle_policy_denial` — a
    re-prompt putting the loop's own directive in front of the reviewer as if it
    had sent one, charged against `max_policy_denials`."""
    orch, config, _, task_store, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2")
    )
    registry.request_urgent("t2", "the operator needs this first")
    task_store.save(registry)

    refuse(orch, "commit_refused")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch._pending_autonomous_revise is None
    assert transcript_entries(config, "autonomous_revise_refused")[0]["reason"] == (
        "urgent_target_pending"
    )


def test_a_revise_the_loop_DECLINED_to_issue_does_not_spend_the_allowance(tmp_path):
    """The meter counts what the loop DID, not what it saw.

    Every refusal in this section parks with the question it always had, and none
    of them is a loop — no round ran, so there is nothing to bound. Spending the
    allowance on one would mean a refusal that was never returned to the agent
    could never be returned at all, which is the automation quietly not
    happening. Here the gate clears and the same refusal gets its one revise."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)
    orch._stat_only_split_review_task = lambda: "t1"  # type: ignore[method-assign]

    refuse(orch, "post_commit_verification_failed")
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert blocker_store.open_blockers()[0].revised_refusals == []

    orch._stat_only_split_review_task = lambda: ""  # type: ignore[method-assign]
    refuse(orch, "post_commit_verification_failed")

    assert orch.state.phase == Phase.READY.value
    assert orch._pending_autonomous_revise is not None
    orch._step(Phase(orch.state.phase))
    assert len(dispatched) == 1
    reasons = [e["reason"] for e in transcript_entries(config, "autonomous_revise_refused")]
    assert reasons == ["stat_only_split_ask_outstanding"]


def test_a_meter_the_loop_cannot_WRITE_parks_rather_than_revising(tmp_path):
    """The fail-open this guard must not have. If the allowance cannot be
    recorded, issuing the round anyway would leave a revise nothing counted —
    and the next identical refusal would find a full allowance, forever. A state
    directory that refuses the write parks with the question it always had."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    dispatched = capture_dispatch(orch)

    def unwritable(blocker_id, fingerprint):
        raise OSError("read-only file system")

    blocker_store.note_refusal_revise = unwritable  # type: ignore[method-assign]

    refuse(orch, "post_commit_verification_failed")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert orch._pending_autonomous_revise is None
    assert dispatched == []
    assert transcript_entries(config, "autonomous_revise_refused")[0]["reason"] == (
        "meter_write_failed:OSError"
    )


def test_a_meter_the_loop_cannot_READ_raises_rather_than_revising(tmp_path):
    """The other direction of the same fail-open. A record whose
    `revised_refusals` is not a list of identities would answer `.count` with a
    substring match or with an AttributeError — so it is refused at `load`, and
    "we cannot read the meter" never reads as "nothing was spent"."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    seeded = blocker_store.record(
        task_id="t1", kind="task_fatal", code="commit_refused", question="q",
        detail="d", phase="ready", now="2026-09-01T00:00:00+00:00",
    )
    path = config.blockers_dir / f"{seeded.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["revised_refusals"] = "not-a-list"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(StateCorruptError):
        refuse(orch, "commit_refused")

    assert orch._pending_autonomous_revise is None


def test_a_pin_granted_after_the_queue_drops_the_revise_rather_than_re_prompting(
    tmp_path,
):
    """The one race the queue-time gate cannot close: `run` drains the operator's
    task inbox BETWEEN steps, so a pin can be granted after the revise was
    queued. Consuming it anyway would let `_dispatch_executor` answer the loop's
    own directive through `_handle_policy_denial` — a re-prompt putting words in
    front of the reviewer as if it had sent them. It is dropped instead, and
    LOGGED: the blocker is already open with its allowance spent, so the next
    occurrence sets the task aside."""
    orch, config, _, task_store, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2")
    )
    dispatched = capture_dispatch(orch)
    refuse(orch, "commit_refused")
    assert orch._pending_autonomous_revise is not None

    registry.request_urgent("t2", "arrived between the two steps")
    task_store.save(registry)
    orch._step(Phase(orch.state.phase))

    assert dispatched == [], "the revise ran ahead of the operator's pin"
    assert orch._pending_autonomous_revise is None
    dropped = transcript_entries(config, "autonomous_revise_dropped")
    assert dropped and dropped[0]["urgent_task_id"] == "t2"


def test_the_urgent_pin_does_not_refuse_a_revise_of_the_pinned_task_itself(tmp_path):
    """The other direction, so the guard is a match rather than a ban: the pin
    asks for THAT task to be dispatched next, and revising it is that."""
    orch, _, _, task_store, registry = build(tmp_path, enabled=True, tasks=("t1", "t2"))
    registry.request_urgent("t1", "the operator needs this first")
    task_store.save(registry)

    refuse(orch, "commit_refused")

    assert orch._pending_autonomous_revise is not None
    assert orch.state.phase == Phase.READY.value


def test_a_second_revise_is_never_queued_behind_a_first(tmp_path):
    """One at a time. A second queued behind the first would run against a task
    whose round has since moved, and the first would vanish."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, tasks=("t1", "t2"))

    refuse(orch, "commit_refused")
    assert orch._pending_autonomous_revise is not None
    first = orch._pending_autonomous_revise
    # A different code, so the meter has a full allowance and only the
    # already-queued check can refuse this.
    refuse(orch, "review_packet_build_failed",
           question="task t1: the packet could not be built")

    assert orch._pending_autonomous_revise == first, "the queued revise was replaced"
    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = [e["reason"] for e in transcript_entries(config, "autonomous_revise_refused")]
    assert reasons == ["revise_already_queued"]


def test_a_zero_ceiling_keeps_the_set_aside_and_issues_no_revise(tmp_path):
    """`max_recovery_attempts = 0` is the operator's off switch for the ACTION
    without switching autonomy off: the set-aside stays, the revise does not
    happen, and no round is run.

    The REASON matters as much as the refusal. A config of zero is not a repeat,
    and logging it as `same_refusal_repeated` would put a claim in the transcript
    that no evidence supports — an operator reading it would go looking for a
    first occurrence that never happened."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True,
                                              max_recovery_attempts=0)

    refuse(orch, "post_commit_verification_failed")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert orch._pending_autonomous_revise is None
    assert "autonomous_recovery" not in transcript_types(config)
    assert transcript_entries(config, "autonomous_revise_refused")[0]["reason"] == (
        "revise_disabled_by_config"
    )
    assert blocker_store.open_blockers()[0].revised_refusals == [], (
        "a refusal the loop declined to answer still spent its one allowance"
    )


def test_without_a_blocker_store_nothing_is_revised(tmp_path):
    """No durable record means no meter to count the resend on, so the loop
    parks exactly as it always did rather than revising unbounded."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, with_store=False)

    refuse(orch, "post_commit_verification_failed")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert orch.state.park_blocker_id is None
    assert orch._pending_autonomous_revise is None


def test_a_non_boolean_enabled_is_not_read_as_consent(tmp_path):
    """A hand-built config is validated by nothing, so the orchestrator's gate is
    `is not True` rather than a truthiness test — and a refusal must never be
    resent because somebody typed `enabled = "no"`."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    object.__setattr__(  # type: ignore[arg-type]
        orch._config, "autonomy", AutonomyConfig(enabled="yes")
    )

    refuse(orch, "post_commit_verification_failed")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch._pending_autonomous_revise is None


def test_an_unusable_execution_record_never_licenses_a_revise(tmp_path):
    """A hand-edited or half-written state file reads as "no round in flight",
    never as agreement with whatever the site named — so nothing is revised and
    the site's terminal stands."""
    for index, execution in enumerate(
        (None, {}, {"task_id": ""}, {"task_id": "   "}, {"task_id": 7}, "not-a-dict")
    ):
        orch, _, _, _, _ = build(tmp_path / f"shape-{index}", enabled=True,
                                 in_flight=None)
        orch.state.task_execution = execution

        refuse(orch, "post_commit_verification_failed")

        assert orch.state.phase == Phase.NEEDS_USER.value, f"{execution!r} revised"
        assert orch._pending_autonomous_revise is None


# =============================================================================
# 7. the hard halts stay hard
# =============================================================================


@pytest.mark.parametrize("code", sorted(HARD_HALT_CODES))
@pytest.mark.parametrize("site_kind", ["loop_fatal", "task_fatal"])
def test_a_hard_halt_is_never_revised_and_keeps_its_own_classification(
    tmp_path, code, site_kind
):
    """BOTH kinds, because the five do not share one today. The property is not
    "it halts the loop" — it is "halt-04 does not touch it": the park is the one
    the site asked for, no revise is queued, and no round is run."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user(f"task t1: {code}", resume_phase=Phase.EXECUTING.value,
                        kind=site_kind, code=code, task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == site_kind, f"{code} was re-classified"
    assert orch.state.resume_phase == Phase.EXECUTING.value
    assert orch._pending_autonomous_revise is None
    assert blocker_store.load(orch.state.park_blocker_id).revised_refusals == []
    assert "autonomous_recovery" not in transcript_types(config)


def test_a_hard_halt_is_not_revised_by_repetition(tmp_path):
    """No number of recurrences turns a hard halt into a revise — the refusal is
    on the code, not on a counter."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    for _ in range(5):
        orch.state.phase = Phase.EXECUTING.value
        orch._to_needs_user("escape", resume_phase=Phase.EXECUTING.value,
                            kind="loop_fatal", code="checkout_escape_detected",
                            task_id="t1")
        assert orch.state.park_kind == "loop_fatal"
        assert orch._pending_autonomous_revise is None
