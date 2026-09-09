"""A lane never gets a task another lane holds, and a HOLD is not a denial —
conc-12.

At `lanes = 2` the fleet died inside ~30 minutes, three times in ninety minutes
on 2026-09-09, and both causes were in the SCHEDULING path rather than in the
fleet code the conc split delivered. One claim, in three parts:

1. **A task one lane holds is never OFFERED to another.** The evidence said the
   registry row read `pending` for the whole time lane 1 was executing ctx-06,
   so `HOLD_IN_FLIGHT` — documented as a backstop against an INSTANT of
   disagreement — became the primary gate and fired on every selection. Section
   2 pins the cause: `Orchestrator._reconcile_stranded_tasks` ran at the top of
   every `_step_ready` with only THIS lane's claim, so a neighbour's live round
   satisfied all four of `health.stranded_fault_rounds`' conditions and was
   RELEASED back to `pending` — with its agent still running. Section 1 pins the
   selector that has to hold the line anyway (`TaskRegistry.set_lane_view`),
   because the two records genuinely do move at different moments, and section 3
   pins the lane view the orchestrator builds from the fleet each round.
2. **A fleet HOLD is not a policy denial.** Section 4: `already_in_flight`,
   `scope_conflict` and `fleet_at_cap` leave `policy_denials` untouched, can
   never reach `policy_denial_budget_exhausted` (loop-fatal, and it took every
   lane down with it), and end the ROUND at a clean boundary once
   `MAX_FLEET_HOLDS` is spent — the same answer the correction asks the reviewer
   for. What is NOT exempt is pinned beside it: a drain, an unreadable fleet and
   a lane a lowered cap cut out are still charged, because none of them has an
   admissible alternative to name.
3. **The alternative a hold NAMED is what the next request offers.** Section 5.
   The denial text already carried it ("Send the same decision for 'notes-02'
   instead") and the next request re-proposed the held task anyway, because the
   roadmap block it was given still offered that task. Asserted on the loop's
   own selector and on the rendered CONTEXT block — never on the denial sentence,
   which is text this loop wrote and reading it back would prove nothing.

And at `lanes = 1` every one of these returns before it reads anything, which is
the acceptance criterion the whole conc split carries. Each section says so with
a test of its own rather than leaving it to the untouched suite.

No git repository, no subprocess and no agent: every claim is about a registry,
a handful of small JSON files and a predicate. The one gateway is a three-method
stub, because `build_context` asks git for a sha, a branch and the dirty list
and this file has nothing to say about any of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoloop import orchestrator
from autoloop.config import AutoloopConfig, BrowserConfig, ConcurrencyConfig
from autoloop.context import build_context, render_context
from autoloop.contract import Decision, Directive
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    FLEET_HOLD_DENIAL_CODE,
    FLEET_HOLD_STOP_KIND,
    FLEET_HOLDS_NOT_CHARGED,
    HOLD_AT_CAP,
    HOLD_IN_FLIGHT,
    HOLD_LANE_RETIRED,
    HOLD_LANE_UNREADABLE,
    HOLD_SCOPE_CONFLICT,
    MAX_FLEET_HOLDS,
    Orchestrator,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    LoopState,
    Phase,
    StateStore,
    lane_state_file,
    utcnow_iso,
)
from autoloop.tasks import CO_SCHEDULE_EXEMPT_PATHS, Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import (
    ATTEMPT_PENDING,
    TaskExecution,
    TaskExecutionStore,
    format_attempt,
)

URL = "https://chatgpt.com/c/lane-hold-scheduling"


def make_config(tmp_path: Path, lanes: int = 2) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        concurrency=ConcurrencyConfig(lanes=lanes),
    )


def a_task(task_id: str, paths=CO_SCHEDULE_EXEMPT_PATHS, **overrides) -> Task:
    """A task whose default scope is the one entry an overlap in is forgiven, so
    a test about the cap measures the cap and never a scope conflict. Taken from
    the constant rather than spelled out, for `test_fleet_supervisor`'s reason."""
    return Task(
        id=task_id,
        title=f"task {task_id}",
        description="a task the supervisor may or may not admit",
        approved_paths=tuple(paths),
        **overrides,
    )


def registry_of(*tasks: Task) -> TaskRegistry:
    registry = TaskRegistry()
    registry.add_many(list(tasks))
    return registry


class StubGit:
    """The three questions `build_context` asks git, and nothing else. This file
    makes no claim about a repository, so a real one would be dead weight — the
    cheapest test that can fail for the right reason."""

    def head_sha(self) -> str:
        return "a" * 40

    def current_branch(self) -> str:
        return "autoloop/mainline"

    def dirty_files(self) -> list[str]:
        return []


def build_lane(
    config: AutoloopConfig,
    tasks=(),
    lane_index: int = 0,
    executions: bool = False,
) -> tuple[Orchestrator, list[str]]:
    """One lane's orchestrator over a real state dir, mid-round, with the
    produce-then-review path doubled — `test_fleet_supervisor.build_lane`, plus
    an optional execution store, which the strand sweep needs to read at all."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(lane_state_file(config.state_dir, lane_index))
    state = LoopState(session_id=f"lane-{lane_index}", conversation_url=URL)
    state.phase = Phase.EXECUTING.value
    store.save(state)
    registry = registry_of(*tasks)
    task_store = TaskStore(config.tasks_file, fleet=config.concurrency.lanes > 1)
    task_store.save(registry)
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: pytest.fail("a scheduling decision opens no chat"),
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        execution_store=(
            TaskExecutionStore(config.executions_dir) if executions else None
        ),
        lane_index=lane_index,
    )
    dispatched: list[str] = []
    orch._dispatch_task_postcommit = lambda d, t, s: dispatched.append(t.id)
    return orch, dispatched


def a_busy_lane(
    config: AutoloopConfig, lane_index: int, task_id: str, dated: bool = False
) -> None:
    """A neighbour lane mid-round on `task_id`, written as that lane's own state
    file — the only thing a supervisor reads about a lane it is not in.

    `dated` writes the pair a live round leaves (`task_execution` naming the
    task and `current_task` stamped now), which is what
    `health.current_round_age_seconds` dates the round from; without both the
    claim has no age and no age is not an exemption."""
    state = LoopState(session_id=f"lane-{lane_index}", conversation_url=URL)
    state.phase = Phase.EXECUTING.value
    state.current_task = {"task_id": task_id, "started_at": utcnow_iso()}
    if dated:
        state.task_execution = {"task_id": task_id}
    StateStore(lane_state_file(config.state_dir, lane_index)).save(state)


def an_open_round(config: AutoloopConfig, task_id: str) -> None:
    """`task_id`'s execution record with an OPEN attempt and no published sha —
    the shape `health.stranded_fault_rounds` reads as "the environment took this
    round", with only the lane claims left to decide it.

    The worktree path is derived from `state_dir` rather than from
    `config.workers_root`, which is `None` on a config built this cheaply — and
    nothing here reads the path anyway: the sweep asks the LEDGER whether the
    round was open, never the filesystem."""
    TaskExecutionStore(config.executions_dir).save(
        TaskExecution(
            task_id=task_id,
            task_branch=f"autoloop/{task_id}",
            worktree_path=str(config.state_dir / "workers" / task_id),
            task_base_sha="0" * 40,
            attempt_ledger=(format_attempt(1, ATTEMPT_PENDING, "dispatched"),),
        )
    )


def implement(task_id: str) -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="next", task_id=task_id)


def transcript_types(config: AutoloopConfig) -> list[str]:
    path = config.transcript_file
    if not path.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def denial_records(config: AutoloopConfig) -> list[dict]:
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r["data"] for r in rows if r["type"] == "policy_denied"]


def stored_status(config: AutoloopConfig, task_id: str) -> str:
    return TaskStore(config.tasks_file).load().get(task_id).status


# ---- 1. the selector -----------------------------------------------------------
#
# `next_ready()` is what the review request OFFERS: `context.build_context` reads
# it for the `next ready:` brief and `registry.summary()` reads it again for the
# roadmap line. A selector that offers a task another lane is running gets that
# task proposed, and the proposal is then held.


def test_a_task_another_lane_holds_is_not_offered_here():
    """THE claim, at the selector. Lane A holds t1; asked from lane B,
    `next_ready()` never returns it — however t1's own row reads, which is the
    half a status cannot make."""
    registry = registry_of(a_task("t1"), a_task("t2"))

    registry.set_lane_view(held_elsewhere={"t1"})

    assert registry.next_ready().id == "t2"
    assert registry.get("t1").status == "pending", "and the row is untouched"


def test_the_supervisor_still_sees_every_ready_task():
    """The fail-open a narrower `ready_in_dispatch_order` would have shipped. The
    supervisor CLASSIFIES the queue, and a task filtered out of it has no hold
    reason at all — which `_refused_outside_fleet_admission` reads as "not this
    gate's business" and lets through. Narrowing the offer must not widen the
    gate."""
    registry = registry_of(a_task("t1"), a_task("t2"))

    registry.set_lane_view(held_elsewhere={"t1"})

    assert [t.id for t in registry.ready_in_dispatch_order()] == ["t1", "t2"]
    assert [t.id for t in registry.ready_tasks()] == ["t1", "t2"]
    plan = orchestrator.FleetSupervisor(2).plan(registry, [], first="t1")
    assert plan.hold_reason("t1") == "" and "t1" in [t.id for t in plan.admitted], (
        "the plan's own answer is unchanged by a lane's view of the queue"
    )


def test_an_empty_view_is_todays_selection():
    """The acceptance criterion, at the selector. Nothing sets a view below two
    lanes, and an empty one is exactly `ready_in_dispatch_order()[0]`."""
    registry = registry_of(a_task("t2"), a_task("t1"))

    assert registry.next_ready().id == "t1"
    registry.set_lane_view()
    assert registry.next_ready().id == "t1"


def test_the_view_is_replaced_rather_than_accumulated():
    """A lane re-reads the fleet every round, and a view that added to itself
    would keep excluding a task the neighbour finished ten rounds ago."""
    registry = registry_of(a_task("t1"), a_task("t2"))

    registry.set_lane_view(held_elsewhere={"t1"})
    registry.set_lane_view(held_elsewhere={"t2"})

    assert registry.next_ready().id == "t1"


def test_a_queue_entirely_held_elsewhere_offers_nothing():
    """The boundary. "Everything ready is running somewhere else" is a real
    answer and must not fall back to offering one of them."""
    registry = registry_of(a_task("t1"), a_task("t2"))

    registry.set_lane_view(held_elsewhere={"t1", "t2"})

    assert registry.next_ready() is None


def test_the_named_alternative_is_offered_ahead_of_the_head():
    """The hint, applied. The hold computed `t3`; the head of the queue is `t2`;
    the next offer is `t3`."""
    registry = registry_of(a_task("t1"), a_task("t2"), a_task("t3"))

    registry.set_lane_view(held_elsewhere={"t1"}, prefer="t3")

    assert registry.next_ready().id == "t3"


@pytest.mark.parametrize(
    "prefer, why",
    [
        ("t1", "an id another lane has taken since"),
        ("gone", "an id the registry has never heard of"),
        ("done", "an id that is no longer READY"),
        ("", "no preference at all"),
    ],
)
def test_a_preference_that_no_longer_holds_is_ignored(prefer, why):
    """VALIDATED, never trusted: the id was computed a round ago and the fleet
    has moved. A stale string must not put a non-offerable task in front of the
    reviewer — it falls through to the head of the queue silently."""
    registry = registry_of(a_task("t1"), a_task("t2"), a_task("done"))
    registry.mark_in_progress("done")

    registry.set_lane_view(held_elsewhere={"t1"}, prefer=prefer)

    assert registry.next_ready().id == "t2", why


def test_the_urgent_pin_outranks_the_hint():
    """A fleet hint may not overrule an operator. `next_ready`'s ordering exists
    to make a pin unbeatable, and a scheduling preference is not an exception to
    it."""
    registry = registry_of(a_task("t1"), a_task("t2"), a_task("t3"))
    registry.request_urgent("t2", "production is down")

    registry.set_lane_view(prefer="t3")

    assert registry.next_ready().id == "t2"


# ---- 2. the strand sweep, which is where the row actually went ------------------
#
# DEFECT 1's cause, measured. `_reconcile_stranded_tasks` runs at the top of every
# `_step_ready` and used to pass only THIS lane's claim: a task the neighbour
# dispatched is `in_progress`, is not this session's current task, has an OPEN
# attempt and no published sha — all four conditions — so the sweep released it to
# `pending` and SAVED. That is why `autoloop tasks` read `0 IN PROGRESS` while a
# lane was executing, and why the in-flight backstop fired on every selection.


def test_a_neighbours_live_round_is_not_swept_back_into_the_queue(tmp_path):
    """THE regression. Lane 1 is executing t2 right now; lane 0 sweeps. The row
    stays `in_progress`, on disk and in memory, and t2 is not offered here."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = build_lane(
        config, tasks=[a_task("t1"), a_task("t2")], executions=True
    )
    orch._registry.mark_in_progress("t2")
    orch._task_store.save(orch._registry)
    an_open_round(config, "t2")
    a_busy_lane(config, 1, "t2", dated=True)

    orch._reconcile_stranded_tasks()

    assert stored_status(config, "t2") == "in_progress", (
        "a lane's live round was requeued under it"
    )
    assert orch._registry.get("t2").status == "in_progress"
    assert "task_strand_requeued" not in transcript_types(config)
    assert orch._registry.next_ready().id == "t1"


def test_a_lane_whose_claim_cannot_be_read_holds_the_sweep(tmp_path):
    """FAIL-CLOSED, and the direction is the whole point: a lane whose claim is
    unknown may be running any of these tasks, so nothing is released and the
    transcript says why. Releasing on no evidence is how a live round is
    requeued."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = build_lane(
        config, tasks=[a_task("t1"), a_task("t2")], executions=True
    )
    orch._registry.mark_in_progress("t2")
    orch._task_store.save(orch._registry)
    an_open_round(config, "t2")
    lane_state_file(config.state_dir, 1).parent.mkdir(parents=True, exist_ok=True)
    lane_state_file(config.state_dir, 1).write_text("{ not json", encoding="utf-8")

    orch._reconcile_stranded_tasks()

    assert stored_status(config, "t2") == "in_progress"
    assert "strand_sweep_held" in transcript_types(config)
    assert "task_strand_requeued" not in transcript_types(config)


def test_a_fleet_scan_that_raises_holds_the_sweep_rather_than_the_round(
    tmp_path, monkeypatch
):
    """`_step_ready` has no `except` around it, so a sweep that raised would end
    the lane with no park, no blocker and no heartbeat — the one exit shape this
    loop went out of its way to eliminate. It holds instead, and says so."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = build_lane(config, tasks=[a_task("t1")], executions=True)
    orch._registry.mark_in_progress("t1")
    orch._task_store.save(orch._registry)
    an_open_round(config, "t1")

    def unreadable(*_args, **_kwargs):
        raise OSError("the state directory went away")

    monkeypatch.setattr(orchestrator, "fleet_lane_claims", unreadable)

    orch._reconcile_stranded_tasks()

    assert stored_status(config, "t1") == "in_progress"
    assert "strand_sweep_held" in transcript_types(config)


def test_at_one_lane_the_sweep_still_requeues_a_real_strand(tmp_path):
    """The other direction, and the one a fix like this most easily breaks: with
    no fleet to ask about, an abandoned round is still returned to the queue —
    which is the whole of strand-01. A lane directory left behind by an
    experiment changes nothing, because the gate is the CAP, read from the config
    in memory."""
    config = make_config(tmp_path, lanes=1)
    orch, _ = build_lane(config, tasks=[a_task("t1")], executions=True)
    orch._registry.mark_in_progress("t1")
    orch._task_store.save(orch._registry)
    an_open_round(config, "t1")
    a_busy_lane(config, 1, "t1", dated=True)  # a lane no single-lane loop reads

    orch._reconcile_stranded_tasks()

    assert stored_status(config, "t1") == "pending", "strand-01's own claim"
    assert "task_strand_requeued" in transcript_types(config)


# ---- 3. the lane view the orchestrator builds -----------------------------------


def test_the_lane_view_excludes_the_task_the_neighbour_names(tmp_path):
    """The race the row cannot answer, which is why the OFFER reads the lanes
    too: lane 1 has taken t1 and its registry row still says `pending`, because
    a lane writes its state file and the row at different moments."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = build_lane(config, tasks=[a_task("t1"), a_task("t2")])
    a_busy_lane(config, 1, "t1")
    assert orch._registry.get("t1").status == "pending", "the race, held still"

    orch._refresh_lane_view()

    assert orch._registry.next_ready().id == "t2"


def test_the_lane_view_adopts_a_row_the_neighbour_persisted(tmp_path):
    """The other source. This registry has been in memory for a whole round, so a
    sibling's `mark_in_progress` landed on disk rather than in this object — and
    an `in_progress` row is not READY, which is the ordinary exclusion."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = build_lane(config, tasks=[a_task("t1"), a_task("t2")])
    orch._registry = orch._task_store.load()
    neighbour = TaskStore(config.tasks_file, fleet=True)
    theirs = neighbour.load()
    theirs.mark_in_progress("t1")
    neighbour.save(theirs)

    orch._refresh_lane_view()

    assert orch._registry.get("t1").status == "in_progress", "adopted, not guessed"
    assert orch._registry.next_ready().id == "t2"


def test_this_lanes_own_slot_is_not_elsewhere(tmp_path):
    """The exclusion that must not exclude the asker. This lane's own state file
    is an occupant of the fleet like any other, and counting it would take this
    lane's own task out of its own offer — every `revise` of the arc it is
    holding included."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = build_lane(config, tasks=[a_task("t1")])
    a_busy_lane(config, 0, "t1")
    orch.state.current_task = {"task_id": "t1", "decision": "implement"}

    orch._refresh_lane_view()

    assert orch._registry.next_ready().id == "t1"


def test_at_one_lane_the_lane_view_reads_nothing(tmp_path, monkeypatch):
    """The acceptance criterion at this call site: below two lanes the view is
    never built, so no file is opened and `next_ready()` is what it always
    was."""
    config = make_config(tmp_path, lanes=1)
    orch, _ = build_lane(config, tasks=[a_task("t1")])
    a_busy_lane(config, 1, "t1")
    monkeypatch.setattr(
        orchestrator,
        "fleet_occupants",
        lambda *a, **k: pytest.fail("a single-lane loop asked the fleet a question"),
    )

    orch._refresh_lane_view()

    assert orch._registry.next_ready().id == "t1"


def test_a_fleet_the_view_cannot_read_narrows_nothing_and_says_so(
    tmp_path, monkeypatch
):
    """This is the one place in the admission story that may fail SOFT, and the
    reasoning is pinned rather than left in a comment: it narrows an OFFER, the
    DISPATCH gate below it still fails closed, and raising here would end the
    round out of `_step_ready`, which catches nothing."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(config, tasks=[a_task("t1", ["autoloop/cli.py"])])

    def unreadable(*_args, **_kwargs):
        raise OSError("the state directory went away")

    monkeypatch.setattr(orchestrator, "fleet_occupants", unreadable)

    orch._refresh_lane_view()

    assert orch._registry.next_ready().id == "t1", "the offer stayed wide"
    assert "lane_view_unreadable" in transcript_types(config)

    orch._dispatch_executor(implement("t1"))

    assert dispatched == [], "and the gate refused it anyway"
    assert HOLD_LANE_UNREADABLE in (orch.state.outbox or "")


# ---- 4. a HOLD is not a denial --------------------------------------------------
#
# `max_policy_denials` bounds a REVIEWER that keeps proposing directives policy
# refuses. A fleet hold is the supervisor declining to double-dispatch, and its
# reason goes away on its own as soon as a lane finishes. Charging it there ended
# the run on `policy_denial_budget_exhausted`, which is loop_fatal and stops every
# lane in the fleet.


def held_lane(config: AutoloopConfig, shape: str):
    """A lane whose next dispatch of `t1` is held for `shape`, and the directive
    that gets held. Three shapes, one per exempt hold word."""
    if shape == HOLD_IN_FLIGHT:
        orch, dispatched = build_lane(
            config, tasks=[a_task("t1"), a_task("t2"), a_task("t3")]
        )
        a_busy_lane(config, 1, "t1")  # named by a lane, row still pending
        return orch, dispatched
    if shape == HOLD_SCOPE_CONFLICT:
        orch, dispatched = build_lane(
            config,
            tasks=[
                a_task("n1", ["autoloop/cli.py"]),
                a_task("t1", ["autoloop/cli.py"]),
                a_task("t3", ["autoloop/health.py"]),
            ],
        )
        orch._registry.mark_in_progress("n1")
        orch._task_store.save(orch._registry)
        a_busy_lane(config, 1, "n1")
        return orch, dispatched
    assert shape == HOLD_AT_CAP, shape
    # Both of a two-lane fleet's slots are held by OTHER lanes (one of them a
    # lane a lowered cap cut out, which still costs a slot), so there is no free
    # lane for anything this session names.
    orch, dispatched = build_lane(
        config,
        tasks=[
            a_task("n1", ["autoloop/a.py"]),
            a_task("n2", ["autoloop/b.py"]),
            a_task("t1", ["autoloop/c.py"]),
        ],
    )
    orch._registry.mark_in_progress("n1")
    orch._registry.mark_in_progress("n2")
    orch._task_store.save(orch._registry)
    a_busy_lane(config, 1, "n1")
    a_busy_lane(config, 3, "n2")
    return orch, dispatched


@pytest.mark.parametrize("shape", FLEET_HOLDS_NOT_CHARGED)
def test_a_fleet_hold_leaves_the_denial_counter_untouched(tmp_path, shape):
    """All three exempt words, driven through the real dispatch site. The task is
    still refused, still queued and still un-attempted — what changes is which
    allowance paid for it."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = held_lane(config, shape)
    before = config.tasks_file.read_bytes()

    orch._dispatch_executor(implement("t1"))

    assert dispatched == [], "the held task did not start"
    assert config.tasks_file.read_bytes() == before, "the registry was not written"
    assert not config.executions_dir.exists(), "no attempt was charged"
    assert orch.state.policy_denials == 0, "a hold is not a denial"
    assert orch.state.fleet_holds == 1
    assert shape in (orch.state.outbox or ""), "and it is named by its word"
    assert Phase(orch.state.phase) is Phase.READY, "corrected, never parked"
    denials = denial_records(config)
    assert [(d["code"], d["fleet_hold"], d["charged"]) for d in denials] == [
        (FLEET_HOLD_DENIAL_CODE, shape, False)
    ]


@pytest.mark.parametrize("shape", FLEET_HOLDS_NOT_CHARGED)
def test_a_run_of_fleet_holds_never_reaches_the_denial_budget(tmp_path, shape):
    """THE failure this task was filed for: three holds in a row raised
    `policy_denial_budget_exhausted` as `loop_fatal` and took every lane down.
    Here the same reviewer sends the same held task past the budget's own ceiling
    and past the fleet's, and the run ends at a CLEAN boundary instead — no
    fault, no blocker, no fleet-fatal terminal."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = held_lane(config, shape)
    ceiling = max(MAX_FLEET_HOLDS, config.policy.max_policy_denials) + 1

    for _ in range(ceiling):
        orch._dispatch_executor(implement("t1"))

    assert dispatched == []
    assert orch.state.policy_denials == 0, "the denial budget was never touched"
    assert "policy_denial_budget_exhausted" not in [
        json.loads(line).get("data", {}).get("code")
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
    ]
    assert not config.blockers_dir.exists(), "a hold files no blocker"
    assert Phase(orch.state.phase) is Phase.STOPPED
    assert orch.state.stop_kind == FLEET_HOLD_STOP_KIND, "clean, not `fault`"
    assert orch.state.fleet_holds > MAX_FLEET_HOLDS
    assert stored_status(config, "t1") == "pending", "and the task is still queued"


def test_the_fleet_hold_stop_is_a_clean_boundary_to_continuous_mode(tmp_path):
    """What "clean" MEANS, asserted through the predicate continuous mode
    actually reads rather than by describing it: `_is_fault_stop` is what routes
    a stopped session to `_report_fault_stop` and exit 2, and this terminal must
    not be one."""
    from autoloop.cli import _is_fault_stop, _is_preemption_stop

    config = make_config(tmp_path, lanes=2)
    orch, _ = held_lane(config, HOLD_IN_FLIGHT)

    for _ in range(MAX_FLEET_HOLDS + 1):
        orch._dispatch_executor(implement("t1"))

    assert Phase(orch.state.phase) is Phase.STOPPED
    assert _is_fault_stop(orch.state) is False
    assert _is_preemption_stop(orch.state) is False
    assert "fleet" in (orch.state.stop_reason or "")


def test_a_hold_does_not_zero_a_denial_streak_the_reviewer_earned(tmp_path):
    """The fail-open the exemption could have introduced. `_step_executing` reads
    "the counter did not move" as "the directive was acted on" and clears the
    streak — so a reviewer alternating one genuinely refused directive with one
    held task would never exhaust the budget. The hold counter is watched
    alongside it for exactly this.

    Driven the way `_step_executing` drives it: read both counters, dispatch,
    hand the pair to `_end_refusal_streak`. Nothing here decides anything — the
    function under test does."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = held_lane(config, HOLD_IN_FLIGHT)
    orch.state.policy_denials = 2
    before = (orch.state.policy_denials, orch.state.fleet_holds)

    orch._dispatch_executor(implement("t1"))
    orch._end_refusal_streak(*before)

    assert orch.state.policy_denials == 2, "the streak stands"
    assert orch.state.fleet_holds == 1


@pytest.mark.parametrize("lanes", [2, 1])
def test_a_hold_with_no_alternative_to_name_is_still_charged(tmp_path, lanes):
    """WHAT IS NOT EXEMPT, and the argument for the line: a lane a lowered cap
    cut out has no admissible alternative at all — its correction asks for
    `stop`, and a reviewer that ignores that is the reviewer `max_policy_denials`
    exists for. Widening the exemption to every `HOLD_*` word is a separate
    decision with a separate argument."""
    config = make_config(tmp_path, lanes=lanes)
    orch, dispatched = build_lane(config, tasks=[a_task("t1")], lane_index=3)

    orch._dispatch_executor(implement("t1"))

    assert dispatched == []
    assert HOLD_LANE_RETIRED in (orch.state.outbox or "")
    assert orch.state.policy_denials == 1
    assert orch.state.fleet_holds == 0


def test_an_operator_quarantine_is_not_a_scheduling_hold(tmp_path):
    """The narrow reading of the claim's own hold, at the atomic-claim site. A
    row moved to `in_progress` under this lane is a sibling taking it; a row
    moved to anything else is an operator's decision the reviewer named, which
    is an ordinary refused directive and is charged like one."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(config, tasks=[a_task("t1")])
    orch._registry = orch._task_store.load()
    orch._registry.mark_in_progress("t1")
    orch._task_store.save(orch._registry)
    orch.state.current_task = {"task_id": "t1", "decision": "implement"}
    quarantining = TaskStore(config.tasks_file, fleet=True)
    theirs = quarantining.load()
    theirs.block("t1", "answer this before it runs again")
    quarantining.save(theirs)

    orch._dispatch_executor(
        Directive(
            decision=Decision.REVISE,
            reason="tighten the claim",
            task_id="t1",
            feedback="one test is asserting the fixture",
        )
    )

    assert dispatched == []
    assert orch.state.policy_denials == 1
    assert orch.state.fleet_holds == 0


def test_a_dispatch_that_goes_through_ends_the_hold_streak(tmp_path):
    """A streak is consecutive by definition. The reviewer sends the alternative,
    the round starts, and the allowance is whole again — otherwise a lane that
    waited out three busy neighbours over a long session would stop for a
    condition that had cleared hours earlier."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = held_lane(config, HOLD_SCOPE_CONFLICT)

    orch._dispatch_executor(implement("t1"))
    assert orch.state.fleet_holds == 1

    before = (orch.state.policy_denials, orch.state.fleet_holds)
    orch._dispatch_executor(implement("t3"))
    orch._end_refusal_streak(*before)  # the pair `_step_executing` reads

    assert dispatched == ["t3"]
    assert orch.state.fleet_holds == 0
    assert orch.state.fleet_hold_alternative == "", "and the hint died with it"


# ---- 5. the alternative the hold named is what is offered next ------------------
#
# The denial TEXT already carried it and the next request re-proposed the held
# task anyway, because the roadmap block in that same request still offered it.
# So the claim is asserted on the loop's own selector and on the RENDERED block —
# never on the sentence the loop wrote, which is an echo and proves nothing.


def test_the_alternative_is_recorded_where_the_next_request_reads_it(tmp_path):
    """The hold computes an admissible task; that name has to survive into the
    next round or the correction is advice nobody acts on."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = held_lane(config, HOLD_SCOPE_CONFLICT)

    orch._dispatch_executor(implement("t1"))

    assert orch.state.fleet_hold_alternative == "t3"


def test_the_next_request_offers_the_alternative_and_not_the_held_task(tmp_path):
    """END TO END, on the block the reviewer is actually given. After the hold,
    the request's `next ready` brief and its roadmap line name the alternative;
    the held task is offered by neither."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = held_lane(config, HOLD_SCOPE_CONFLICT)
    orch._dispatch_executor(implement("t1"))

    orch._refresh_lane_view()
    ctx = build_context(
        orch.state, StubGit(), orch._registry, "alr-test-0002", "the next packet"
    )
    block = render_context(ctx)

    assert orch._registry.next_ready().id == "t3"
    assert ctx.next_ready is not None and ctx.next_ready.task_id == "t3"
    assert "next ready: t3" in ctx.roadmap_status
    assert "next ready: t1" not in block
    assert "t3" in block


def test_an_alternative_taken_since_is_not_offered_anyway(tmp_path):
    """The stale hint, at the site that produced it. Between the hold and the
    next request another lane took t3; the offer falls through to the queue
    rather than naming a task that is now held."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = held_lane(config, HOLD_SCOPE_CONFLICT)
    orch._dispatch_executor(implement("t1"))
    a_busy_lane(config, 3, "t3")  # a third lane took the alternative meanwhile

    orch._refresh_lane_view()

    assert orch.state.fleet_hold_alternative == "t3", "the record is not rewritten"
    assert orch._registry.next_ready().id == "t1", (
        "the hint is ignored and the head of the queue answers"
    )


def test_a_hold_with_no_alternative_clears_the_one_before_it(tmp_path):
    """A name left behind by an earlier hold would keep steering the queue after
    the condition that produced it was gone. Assigned unconditionally, empty
    included."""
    config = make_config(tmp_path, lanes=2)
    orch, _ = held_lane(config, HOLD_SCOPE_CONFLICT)
    orch._dispatch_executor(implement("t1"))
    assert orch.state.fleet_hold_alternative == "t3"

    # A second hold with nothing admissible left to name: t3 is now running in a
    # lane of its own, so the fleet is at its cap.
    a_busy_lane(config, 3, "t3")
    orch._registry.mark_in_progress("t3")
    orch._task_store.save(orch._registry)
    orch._dispatch_executor(implement("t1"))

    assert orch.state.fleet_hold_alternative == ""
    assert "Nothing else may start in this lane" in (orch.state.outbox or "")
