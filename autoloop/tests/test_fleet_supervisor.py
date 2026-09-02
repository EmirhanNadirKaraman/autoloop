"""The fleet supervisor — conc-06.

Candidate 5 of the nine in docs/AUTOLOOP.md, "Running several tasks at once —
the split plan". One claim, in five parts:

**The supervisor owns scheduling across N lanes, enforces the cap and the
admission rule, and reaches a self-upgrade boundary by draining.**

1. **The cap is a cap, and holding costs the held task NOTHING.** "Exactly
   `lanes` are dispatched and the rest stay `pending` with no attempt charged"
   is asserted as a BYTE COMPARISON of the whole state directory around
   `plan()`, not only as a status: attempts live in `executions/<task>.json`
   under that directory, so a snapshot is what can see one being charged, and a
   status equality cannot.
2. **The admission rule is Decision 3's, exactly.** Declared paths only, with
   the six universal trackers and `CO_SCHEDULE_EXEMPT_PATHS` — the shared test
   tree — the only entries an overlap in is forgiven. So two tasks sharing
   `autoloop/tests/` and every tracker co-schedule, and two naming one file, or
   one directory that is not that tree, do not. The gate is fed `TRACKER_PATHS`
   and `CO_SCHEDULE_EXEMPT_PATHS` themselves rather than copies of their
   entries, because a copy agrees on the day it is written.
3. **The drain REACHES the boundary.** Withholding admission alone would leave
   the merged upgrade `pending` while the loop slept beside it forever — the
   silent-no-outcome failure the plan says concurrency must not reintroduce —
   so section 4 drives `cli._run_continuous` itself, with a lane that finishes
   between two iterations, and asserts the boundary was taken on the tick after
   the fleet emptied.
4. **And at `lanes = 1` nothing consults any of it.** `cli._fleet_plan` answers
   `None` there, which is the acceptance criterion made structural; section 5
   drives the same continuous loop at one lane, with a pending upgrade and more
   ready tasks than lanes, and pins that the selection is still reached.
5. **The plan governs DISPATCH, not only whether a session opens.** Section 6
   drives `Orchestrator._dispatch_executor` — the site a reviewer's directive
   becomes a round — and pins that a task the plan held cannot start there while
   an admitted one can, that the answer is about the fleet rather than about
   queue position, and that a lane whose fleet state cannot be read refuses
   instead of dispatching blind.

No git repository, no subprocess and no agent: every claim here is about a
registry, a small JSON file and a predicate. The two places the real loop is
needed are the wiring — section 4 drives `cli._run_continuous` with only the
phase machine and the replacement itself doubled — and the dispatch site, where
section 6 drives the real `Orchestrator._dispatch_executor` and doubles only
`_dispatch_task_postcommit`, the produce-then-review path a worker repo, a
commit and an attempt all come from.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from autoloop import cli, orchestrator
from autoloop.auto_merge import (
    UPGRADE_EXEC_FAILED,
    UPGRADE_PENDING,
    PendingUpgrade,
    UpgradeStore,
)
from autoloop.config import AutoloopConfig, BrowserConfig, ConcurrencyConfig, lane_id
from autoloop.contract import Decision, Directive
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    FLEET_HOLD_DENIAL_CODE,
    HOLD_AT_CAP,
    HOLD_DRAINING,
    HOLD_IN_FLIGHT,
    HOLD_LANE_UNREADABLE,
    HOLD_SCOPE_CONFLICT,
    FleetSupervisor,
    LaneOccupant,
    Orchestrator,
    lane_occupants,
    session_task_id,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.transcript import TranscriptLogger
from autoloop.state import (
    LoopState,
    Phase,
    StateStore,
    lane_paths,
    lane_state_file,
    utcnow_iso,
)
from autoloop.tasks import (
    CO_SCHEDULE_EXEMPT_PATHS,
    TRACKER_PATHS,
    Task,
    TaskRegistry,
    TaskStore,
    co_schedule_conflict,
    declared_scope_entries,
    effective_approved_paths,
)

URL = "https://chatgpt.com/c/fleet-supervisor-test"

#: Two of the six universal trackers, taken from the list itself rather than
#: spelled out. Two reasons, and the second is mechanical: a copy agrees with
#: `TRACKER_PATHS` on the day it is written, and a test file that names a
#: tracker in CODE while also resolving its own checkout (`__file__`) is
#: attributed as a READER of that document by `validation._files_reading_
#: documents` — which would put this file in every docs-only round's selection
#: and move a measured number two other test files pin. Nothing here needs the
#: real checkout, so neither condition is met.
A_TRACKER, ANOTHER_TRACKER = TRACKER_PATHS[0], TRACKER_PATHS[-1]

#: What a self-upgrade record names as the tree it was merged into. Every test
#: below doubles `_self_upgrade_at_boundary`, so applicability is never reached
#: and this need not be — and must not be — this process's own checkout.
A_REPO_ROOT = "/not/a/real/checkout"


def make_config(tmp_path: Path, lanes: int = 2) -> AutoloopConfig:
    """The cheapest real config: `state_dir` is all these claims read."""
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        concurrency=ConcurrencyConfig(lanes=lanes),
    )


def a_task(task_id: str, paths=CO_SCHEDULE_EXEMPT_PATHS, **overrides) -> Task:
    """A task whose default scope is the ONE entry an overlap in is forgiven, so
    every test about the cap measures the cap and never a scope conflict. Taken
    from the constant rather than spelled out: the day that list changes, these
    tests follow it instead of quietly starting to measure the gate."""
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


def tree_bytes(root: Path) -> dict[str, bytes]:
    """Every file under `root`, by relative path. The evidence for "no attempt
    charged": an attempt is a write under the state dir, and this sees any."""
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def busy(lane_index: int, task_id: str | None = None) -> LaneOccupant:
    """A lane the supervisor is told is running something. The id comes from
    `config.lane_id`, never a second spelling of the prefix."""
    return LaneOccupant(lane_index, lane_id(lane_index), task_id)


def admitted_ids(plan) -> list[str]:
    return [task.id for task in plan.admitted]


# ---- 1. the cap ---------------------------------------------------------------


def test_more_ready_tasks_than_lanes_admits_exactly_lanes(tmp_path):
    """THE cap. Four ready tasks, two lanes: two start and two wait, and the two
    that wait are `pending`, un-attempted and named with the reason."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(
        a_task("t1"), a_task("t2"), a_task("t3"), a_task("t4")
    )
    TaskStore(config.tasks_file).save(registry)
    before = tree_bytes(config.state_dir)

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == ["t1", "t2"]
    assert plan.held == (("t3", HOLD_AT_CAP), ("t4", HOLD_AT_CAP))
    assert [t.status for t in registry.all_tasks()] == ["pending"] * 4
    assert tree_bytes(config.state_dir) == before, (
        "planning wrote something — an attempt, a status or a state file"
    )
    assert not config.executions_dir.exists(), "no attempt was charged"


def test_a_busy_lane_costs_a_slot(tmp_path):
    """Two lanes with one already running admit exactly one more. The task the
    live lane holds is never admitted a second time, whatever the registry says
    about it — the two records are written at different moments."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"), a_task("t2"), a_task("t3"))

    plan = FleetSupervisor.from_config(config).plan(registry, [busy(1, "t1")])

    assert admitted_ids(plan) == ["t2"]
    assert plan.hold_reason("t1") == HOLD_IN_FLIGHT
    assert plan.hold_reason("t3") == HOLD_AT_CAP
    assert plan.free_lanes == 1 and not plan.fleet_idle


def test_an_in_progress_row_is_never_admitted_again(tmp_path):
    """The registry's own view of what is in flight, for the round whose lane
    has not written its state file yet (or whose process died). `state_of`
    already keeps an `in_progress` row out of READY; this is the belt on top of
    it, and it is what stops one task running in two lanes."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"), a_task("t2"))
    registry.mark_in_progress("t1")

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == ["t2"]
    assert plan.hold_reason("t1") == "", "an in-progress row is not READY at all"


def test_the_cap_lowered_under_a_running_fleet_admits_nothing(tmp_path):
    """Three occupants against two lanes is `lanes` edited while the fleet ran.
    The answer is zero free lanes, never a negative one — which would have
    `len(admitted) >= free` admit the whole queue.

    Every occupant names a task the registry describes, so what is measured here
    is the CAP and not the fail-closed hold an unknown scope would produce."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"), a_task("x"), a_task("y"), a_task("z"))

    plan = FleetSupervisor.from_config(config).plan(
        registry, [busy(0, "x"), busy(1, "y"), busy(2, "z")]
    )

    assert plan.free_lanes == 0
    assert plan.admitted == ()
    assert plan.hold_reason("t1") == HOLD_AT_CAP
    assert plan.hold_reason("x") == HOLD_IN_FLIGHT


def test_the_queue_order_is_next_ready_s_own(tmp_path):
    """One ordering, two callers. The urgent pin outranks priority and the id
    tiebreak — asserted through the SUPERVISOR, because a scheduler that sorted
    for itself is how the pin quietly stops being honoured."""
    config = make_config(tmp_path, lanes=1)
    registry = registry_of(
        a_task("brw-13", priority=1), a_task("codex-01", priority=1)
    )
    registry.request_urgent("codex-01", "transport down")

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == ["codex-01"]
    assert [t.id for t in registry.ready_in_dispatch_order()] == [
        "codex-01",
        "brw-13",
    ]
    assert registry.next_ready().id == "codex-01"


def test_at_one_lane_the_plan_is_todays_selection(tmp_path):
    """`lanes = 1` is the loop as it runs today, stated as an equality rather
    than as a description: the supervisor admits exactly `next_ready()`."""
    config = make_config(tmp_path, lanes=1)
    registry = registry_of(a_task("t2", priority=2), a_task("t1", priority=1))

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == [registry.next_ready().id] == ["t1"]
    assert plan.hold_reason("t2") == HOLD_AT_CAP


def test_an_empty_queue_admits_nothing_and_holds_nothing(tmp_path):
    config = make_config(tmp_path, lanes=2)

    plan = FleetSupervisor.from_config(config).plan(TaskRegistry())

    assert plan.admitted == () and plan.held == ()
    assert plan.fleet_idle and not plan.draining and not plan.upgrade_boundary


@pytest.mark.parametrize("lanes", [0, -1, True, 1.0, "2", None])
def test_a_fleet_size_nobody_can_name_is_refused(lanes):
    """`load_config` refuses these at load time; a supervisor built by hand must
    not be the one place a fleet of zero — or of `True` — exists."""
    with pytest.raises(ValueError):
        FleetSupervisor(lanes)


# ---- 2. the admission rule ----------------------------------------------------


def test_two_tasks_declaring_one_file_are_not_co_scheduled(tmp_path):
    """The gate, in the words of Decision 3: a same-file declaration is the
    strongest advance signal that both lanes will edit the same file."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(
        a_task("t1", ["autoloop/cli.py", "autoloop/tests/"]),
        a_task("t2", ["autoloop/cli.py", "autoloop/health.py"]),
    )

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == ["t1"]
    assert plan.hold_reason("t2").startswith(HOLD_SCOPE_CONFLICT)
    assert "t1" in plan.hold_reason("t2")
    assert "autoloop/cli.py" in plan.hold_reason("t2"), "name the file, not just the task"
    assert registry.state_of("t2").value == "ready", "held, never blocked"


def test_the_shared_test_tree_and_the_trackers_do_not_gate(tmp_path):
    """The other half, and the half that decides whether the fleet moves at all:
    nearly every task declares `autoloop/tests/` and every task is granted the
    six trackers, so gating on either would serialise the fleet and buy
    nothing. THOSE entries, from the constants themselves — not "a directory
    entry", which is the wider rule the test below pins the refusal of."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(
        a_task("t1", ["autoloop/cli.py", *CO_SCHEDULE_EXEMPT_PATHS, *TRACKER_PATHS]),
        a_task("t2", ["autoloop/health.py", *CO_SCHEDULE_EXEMPT_PATHS, *TRACKER_PATHS]),
    )

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == ["t1", "t2"]
    assert plan.held == ()


@pytest.mark.parametrize("shared", ["autoloop/", "docs/", "autoloop/audit/"])
def test_a_shared_directory_that_is_not_the_test_tree_does_gate(tmp_path, shared):
    """The correction this candidate's second round exists for. Dropping every
    entry that ends in a slash co-scheduled two tasks that each declare
    `autoloop/` — two scopes each authorized to write every file the other one
    touches, which is the case the gate exists for. The exemption is the
    enumerated `CO_SCHEDULE_EXEMPT_PATHS`, so a directory outside it gates
    exactly like a file."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(
        a_task("t1", [shared, *CO_SCHEDULE_EXEMPT_PATHS, *TRACKER_PATHS]),
        a_task("t2", [shared, *CO_SCHEDULE_EXEMPT_PATHS, *TRACKER_PATHS]),
    )

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == ["t1"]
    assert plan.hold_reason("t2") == f"{HOLD_SCOPE_CONFLICT}: t1 ({shared})"
    assert registry.state_of("t2").value == "ready", "held, never blocked"


def test_a_file_inside_the_exempt_tree_gates_like_any_other_file(tmp_path):
    """The exemption is an EXACT entry, never a prefix. Two tasks that both name
    one test file are two tasks that will both edit it, and the reason the tree
    itself is forgiven — different files under it do not collide — does not
    reach that case."""
    config = make_config(tmp_path, lanes=2)
    # Built from the exempt entry, and deliberately not the name of a file that
    # exists: what is measured is the string rule, and a real path would tie this
    # assertion to a test file that may be renamed for reasons of its own.
    inside = f"{CO_SCHEDULE_EXEMPT_PATHS[0]}test_a_file_this_suite_does_not_have.py"
    registry = registry_of(
        a_task("t1", [inside, *CO_SCHEDULE_EXEMPT_PATHS]),
        a_task("t2", [inside, *CO_SCHEDULE_EXEMPT_PATHS]),
    )

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == ["t1"]
    assert plan.hold_reason("t2") == f"{HOLD_SCOPE_CONFLICT}: t1 ({inside})"


def test_a_conflict_with_a_LIVE_lane_holds_too(tmp_path):
    """The gate is not only about this tick's pair: a lane already running t1 is
    a scope the fleet is committed to, and the conflicting task waits for it."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(
        a_task("t1", ["autoloop/cli.py"]),
        a_task("t2", ["autoloop/cli.py"]),
        a_task("t3", ["autoloop/health.py"]),
    )
    registry.mark_in_progress("t1")

    plan = FleetSupervisor.from_config(config).plan(registry, [busy(0, "t1")])

    assert admitted_ids(plan) == ["t3"]
    assert plan.hold_reason("t2").startswith(HOLD_SCOPE_CONFLICT)


def test_the_conflict_reason_is_deterministic(tmp_path):
    """Two live lanes both conflicting: the reported one is the lower id, so the
    transcript does not depend on dict iteration order."""
    config = make_config(tmp_path, lanes=4)
    registry = registry_of(
        a_task("a1", ["autoloop/cli.py"]),
        a_task("b2", ["autoloop/cli.py"]),
        a_task("c3", ["autoloop/cli.py"]),
    )
    registry.mark_in_progress("a1")
    registry.mark_in_progress("b2")

    plan = FleetSupervisor.from_config(config).plan(
        registry, [busy(0, "b2"), busy(1, "a1")]
    )

    assert plan.hold_reason("c3") == f"{HOLD_SCOPE_CONFLICT}: a1 (autoloop/cli.py)"


def test_the_gate_reads_declared_paths_less_the_ungated_ones():
    """The predicate itself, without a supervisor around it. An entry is dropped
    by IDENTITY against `TRACKER_PATHS` and `CO_SCHEDULE_EXEMPT_PATHS` rather
    than by a copy of their entries or by a shape rule about trailing slashes —
    every other declared entry, directory or file, gates."""
    exempt_tree = CO_SCHEDULE_EXEMPT_PATHS[0]
    declared = ("autoloop/cli.py", exempt_tree, A_TRACKER)

    assert declared_scope_entries(declared) == frozenset({"autoloop/cli.py"})
    assert declared_scope_entries(("autoloop/", "docs/")) == frozenset(
        {"autoloop/", "docs/"}
    ), "a directory outside the exempt list is a gating entry"
    # Both sides declare the SAME tracker and the SAME test tree, which is the
    # shape every pair of tasks in this repository has, and still co-schedule.
    assert co_schedule_conflict(declared, (exempt_tree, A_TRACKER)) == ()
    assert co_schedule_conflict(declared, (ANOTHER_TRACKER,)) == ()
    assert co_schedule_conflict(declared, ("autoloop/cli.py",)) == ("autoloop/cli.py",)
    assert co_schedule_conflict(("docs/",), ("docs/",)) == ("docs/",)
    # Symmetric, and sorted, so the answer cannot depend on which lane asked.
    assert co_schedule_conflict(("b.py", "a.py"), ("a.py", "b.py")) == ("a.py", "b.py")
    assert co_schedule_conflict(("a.py",), ("b.py", "a.py")) == co_schedule_conflict(
        ("b.py", "a.py"), ("a.py",)
    )
    # The exempt list is exactly the argument that justifies it: one entry, the
    # shared test tree, and nothing that is also universally granted.
    assert CO_SCHEDULE_EXEMPT_PATHS == ("autoloop/tests/",)
    assert not set(CO_SCHEDULE_EXEMPT_PATHS) & set(TRACKER_PATHS)


def test_containment_is_a_stated_residual_the_merge_protocol_owns():
    """Entries are compared for EQUALITY, so a broad scope and a narrow one
    inside it are not an overlap here — `autoloop/` beside `autoloop/cli.py`
    co-schedules, though both lanes may write that file.

    Pinned so it reads as a decision rather than an oversight. Decision 3 owns
    that overlap where it is owned: the per-diff scope gate, the serialised
    merge and the re-review obligation, which handle two lanes touching one file
    as a COST. A containment rule would instead make every whole-package scope
    conflict with every narrow one and serialise the fleet — buying that cost
    back at the price of the concurrency the fleet exists for."""
    assert co_schedule_conflict(("autoloop/",), ("autoloop/cli.py",)) == ()
    assert co_schedule_conflict(("autoloop/", "docs/"), ("autoloop/",)) == ("autoloop/",)


def test_an_unscoped_task_conflicts_with_nothing_and_is_still_refused_at_dispatch(tmp_path):
    """A task with no declared scope has no file entries, so the ADMISSION gate
    lets it through — and that is deliberate: dispatch refuses it separately
    (`effective_approved_paths` answers `()`, docs/SECURITY.md finding #2), and
    an efficiency gate must not become a second, weaker copy of a security one.
    Pinned so the reading is a decision rather than an accident."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1", []), a_task("t2", []))

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert admitted_ids(plan) == ["t1", "t2"]
    assert effective_approved_paths(()) == (), "and neither may write anything"


# ---- 3. what the supervisor refuses to guess ----------------------------------


def test_an_unreadable_lane_stops_admission_rather_than_raising_the_cap(tmp_path):
    """FAIL-CLOSED, the direction that matters most here. A lane whose state
    cannot be read might hold anything, so the fleet admits nothing beside it —
    a supervisor that skipped what it could not read would silently raise the
    effective cap on exactly the lane that is in trouble."""
    config = make_config(tmp_path, lanes=2)
    lane_one = lane_paths(config.state_dir, 1).state_file
    lane_one.parent.mkdir(parents=True, exist_ok=True)
    lane_one.write_text("{ this is not json", encoding="utf-8")
    registry = registry_of(a_task("t1"))

    occupants = lane_occupants(config)
    plan = FleetSupervisor.from_config(config).plan(registry, occupants)

    assert [o.readable for o in occupants] == [False]
    assert occupants[0].lane_index == 1
    assert plan.admitted == ()
    assert plan.hold_reason("t1") == HOLD_LANE_UNREADABLE


def test_a_lane_holding_a_task_the_registry_cannot_describe_stops_admission(tmp_path):
    """The same fail-closed rule one level down: the gate needs the occupant's
    declared scope to answer, and an id no row describes has none. Admitting
    beside it would be a gate that passes because what it needs is missing."""
    config = make_config(tmp_path, lanes=3)
    registry = registry_of(a_task("t1", ["autoloop/cli.py"]))

    plan = FleetSupervisor.from_config(config).plan(registry, [busy(0, "vanished")])

    assert plan.admitted == ()
    assert plan.hold_reason("t1") == HOLD_LANE_UNREADABLE


def test_a_lane_that_names_no_task_costs_a_slot_and_nothing_else(tmp_path):
    """A fresh session on the audit kickoff names no task. That is an ordinary
    state, not a fault: it costs the lane it is in, and constrains the scope of
    nothing."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1", ["autoloop/cli.py"]))

    plan = FleetSupervisor.from_config(config).plan(registry, [busy(0, None)])

    assert admitted_ids(plan) == ["t1"]
    assert plan.free_lanes == 1


def test_lane_occupancy_is_read_from_the_lanes_own_state_files(tmp_path):
    """Busy is "the state file exists and its phase is not terminal" — the same
    test `_run_continuous` applies to its own session, so a lane the loop would
    treat as at a clean boundary is a lane this counts as free."""
    config = make_config(tmp_path, lanes=4)
    mid_round = LoopState(session_id="s1", conversation_url=URL)
    mid_round.phase = Phase.EXECUTING.value
    mid_round.current_task = {"task_id": "t1"}
    StateStore(lane_state_file(config.state_dir, 1)).save(mid_round)
    finished = LoopState(session_id="s2", conversation_url=URL)
    finished.phase = Phase.STOPPED.value
    StateStore(lane_state_file(config.state_dir, 2)).save(finished)

    occupants = lane_occupants(config)

    assert [(o.lane_index, o.task_id) for o in occupants] == [(1, "t1")]
    assert occupants[0].lane_id == lane_paths(config.state_dir, 1).lane_id
    assert all(o.readable for o in occupants)


def test_a_session_whose_two_records_disagree_names_no_task(tmp_path):
    """`Orchestrator._active_task_id`'s rule, asked from outside: a session
    whose execution record and dispatch record name different tasks is one the
    loop cannot attribute, and a guess would be a scope check against the wrong
    row. It costs a lane instead."""
    state = LoopState(session_id="s", conversation_url=URL)
    state.current_task = {"task_id": "t1"}
    state.task_execution = {"task_id": "t2"}

    assert session_task_id(state) is None

    state.task_execution = {"task_id": "t1"}
    assert session_task_id(state) == "t1"
    state.current_task = {"nothing": "useful"}
    assert session_task_id(state) == "t1"


def test_an_unknown_phase_reads_as_busy_not_as_free(tmp_path):
    """A state file this build cannot classify is not evidence of an idle lane.
    Written past `StateStore.save` on purpose — the phase is what is unknown,
    not the JSON."""
    config = make_config(tmp_path, lanes=2)
    path = lane_state_file(config.state_dir, 1)
    StateStore(path).save(LoopState(session_id="s", conversation_url=URL))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["phase"] = "teleporting"
    path.write_text(json.dumps(data), encoding="utf-8")

    occupants = lane_occupants(config)

    assert len(occupants) == 1 and occupants[0].readable is False


# ---- 4. the drain, and the boundary it reaches --------------------------------


class StopTheLoop(Exception):
    """Ends `_run_continuous` from inside a fake sleep or a fake boundary —
    reaching it at all is half of every assertion below."""


def upgrade_config(tmp_path: Path, lanes: int = 2) -> AutoloopConfig:
    """A config carrying a `pending` upgrade record — the input the drain reads.
    Its `repo_root` is deliberately not this checkout (see `A_REPO_ROOT`):
    every test here doubles the boundary itself, so applicability is decided by
    nothing below."""
    config = make_config(tmp_path, lanes=lanes)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    UpgradeStore(config.pending_upgrade_file).save(
        PendingUpgrade(
            base_sha="b" * 40,
            previous_base_sha="a" * 40,
            candidate_sha="c" * 40,
            task_id="conc-06",
            repo_root=A_REPO_ROOT,
            paths=["autoloop/orchestrator.py"],
            status=UPGRADE_PENDING,
            recorded_at=utcnow_iso(),
        )
    )
    return config


def continuous_args() -> argparse.Namespace:
    return argparse.Namespace(config=None, continuous=True, null_executor=False)


def transcript_types(config: AutoloopConfig) -> list[str]:
    path = config.transcript_file
    if not path.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def denial_codes(config: AutoloopConfig) -> list[str]:
    """The verdict code of every refused directive, so a fleet hold is told
    apart from every other refusal by a value rather than by prose."""
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r["data"]["code"] for r in rows if r["type"] == "policy_denied"]


def fleet_hold_entries(config: AutoloopConfig) -> list[dict]:
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r["data"] for r in rows if r["type"] == "fleet_hold"]


def test_a_pending_upgrade_stops_admission_and_the_boundary_waits_for_the_last_lane(
    tmp_path, monkeypatch
):
    """THE drain, driven through the real `cli._run_continuous`.

    Iteration one: an upgrade is pending and lane 1 is mid-round, so nothing is
    admitted and no boundary is taken — the live lane must finish first. The
    fake sleep is where that lane finishes, which is how one test covers both
    halves of "stops admission" and "once the last lane finishes".

    Iteration two: the fleet is idle, so the boundary is REACHED rather than
    waited for. Nothing about the ready queue changed between the two — only
    the neighbour's phase.
    """
    config = upgrade_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1"), a_task("t2")))
    lane_one = StateStore(lane_state_file(config.state_dir, 1))
    mid_round = LoopState(session_id="lane-one", conversation_url=URL)
    mid_round.phase = Phase.EXECUTING.value
    mid_round.current_task = {"task_id": "t1"}
    lane_one.save(mid_round)
    monkeypatch.setattr(
        cli,
        "_select_and_kickoff",
        lambda *a, **k: pytest.fail("a drain must admit nothing"),
    )
    boundaries: list = []

    def reached(cfg, lock, args=None, lane=None):
        boundaries.append(cfg.pending_upgrade_file)
        raise StopTheLoop()

    monkeypatch.setattr(cli, "_self_upgrade_at_boundary", reached)

    def finish_lane_one(seconds):
        # Matched by duration, the way `test_self_upgrade` matches it: an
        # unrelated internal sleep must not be mistaken for the fleet poll and
        # finish the neighbour a step early.
        if seconds != cli.CONTINUOUS_POLL_SECONDS:
            return
        assert boundaries == [], "the boundary was taken with a lane still running"
        done = lane_one.load()
        done.phase = Phase.STOPPED.value
        lane_one.save(done)

    monkeypatch.setattr(cli.time, "sleep", finish_lane_one)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)

    assert len(boundaries) == 1, "the boundary is reached once the fleet is empty"
    held = fleet_hold_entries(config)
    assert held and held[0]["draining"] is True
    assert HOLD_DRAINING in held[0]["held"]
    assert held[0]["busy"] == 1 and held[0]["lanes"] == 2


def test_the_drain_holds_every_ready_task_and_charges_none(tmp_path):
    """The supervisor half of the same claim, without the loop around it."""
    config = make_config(tmp_path, lanes=3)
    registry = registry_of(a_task("t1"), a_task("t2"))

    plan = FleetSupervisor.from_config(config).plan(
        registry, [busy(0, "t9")], upgrade_pending=True
    )

    assert plan.admitted == ()
    assert plan.draining and not plan.fleet_idle and not plan.upgrade_boundary
    assert plan.hold_reason("t1") == HOLD_DRAINING
    assert [t.status for t in registry.all_tasks()] == ["pending", "pending"]


def test_the_boundary_is_reached_with_an_empty_queue_too(tmp_path, monkeypatch):
    """The case a "hold every ready task" drain would miss entirely: nothing is
    ready, so nothing is held, and a supervisor that only withheld admission
    would sleep beside the merged code forever."""
    config = upgrade_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry())
    monkeypatch.setattr(
        cli, "_select_and_kickoff", lambda *a, **k: pytest.fail("no audit during a drain")
    )

    def reached(*_args, **_kwargs):
        raise StopTheLoop()

    monkeypatch.setattr(cli, "_self_upgrade_at_boundary", reached)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)


def test_a_boundary_that_cannot_hand_off_lets_the_fleet_admit_again(
    tmp_path, monkeypatch
):
    """THE spin this design would otherwise have. The three non-exec outcomes
    leave the record `pending` on purpose, so a drain keyed on the record alone
    would empty the fleet, fail to hand off, and drain again forever. The sha is
    declined into this run's answered set, and the very next tick selects."""
    config = upgrade_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1")))
    monkeypatch.setattr(
        cli, "_self_upgrade_at_boundary", lambda *a, **k: UPGRADE_EXEC_FAILED
    )
    selected: list = []

    def select(cfg, store, registry):
        selected.append(registry.next_ready().id)
        raise StopTheLoop()

    monkeypatch.setattr(cli, "_select_and_kickoff", select)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)

    assert selected == ["t1"], "the fleet admitted again after the refused handoff"
    record = UpgradeStore(config.pending_upgrade_file).load()
    assert record.status == UPGRADE_PENDING, "still retryable by the next process"


def test_an_upgrade_record_with_no_usable_sha_does_not_drain(tmp_path):
    """A record the boundary itself would refuse (`upgrade_bound_sha` answers
    `""`) must not stop the fleet: it would drain, be refused, and drain
    again. Same predicate on both sides, so a record one refuses is a record
    the other does not wait for."""
    config = upgrade_config(tmp_path)
    record = UpgradeStore(config.pending_upgrade_file).load()
    record.base_sha = ""
    UpgradeStore(config.pending_upgrade_file).save(record)

    assert cli._drainable_upgrade_sha(config, set()) == ""

    record.base_sha = "b" * 40
    UpgradeStore(config.pending_upgrade_file).save(record)
    assert cli._drainable_upgrade_sha(config, set()) == "b" * 40
    assert cli._drainable_upgrade_sha(config, {"b" * 40}) == ""


def test_an_unreadable_upgrade_record_does_not_stop_the_fleet(tmp_path):
    """`UpgradeStore.load` answers `None` for a record it cannot read, and that
    is the right direction here as well: an unreadable marker means "do not stop
    dispatching", and the merged code is on disk either way."""
    config = make_config(tmp_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.pending_upgrade_file.write_text("{ not json", encoding="utf-8")

    assert cli._drainable_upgrade_sha(config, set()) == ""


# ---- 5. and at one lane, nothing consults any of it ---------------------------


def test_only_a_continuous_fleet_gives_up_the_per_round_boundary(tmp_path):
    """The other half of "reaches the boundary BY DRAINING": a continuous run
    above one lane turns the per-round boundary off, so a lane cannot replace
    the process while its neighbours are mid-round.

    Every other shape keeps today's `True`, and the single-round one is the
    sharp case: `_run_locked` has no supervisor and can never reach a drain, so
    switching it off there would leave a merged upgrade with nothing able to act
    on it — silence, which is what the drain exists to END. It defers out loud
    instead."""
    one, fleet = make_config(tmp_path, lanes=1), make_config(tmp_path, lanes=2)
    single_round = argparse.Namespace(config=None, continuous=False)

    assert cli._round_boundary_may_upgrade(one, continuous_args()) is True
    assert cli._round_boundary_may_upgrade(one, single_round) is True
    assert cli._round_boundary_may_upgrade(fleet, single_round) is True
    assert cli._round_boundary_may_upgrade(fleet, continuous_args()) is False
    # An args namespace that has never heard of `--continuous` (every embedder,
    # and `test_m1_hardening`'s) is the single-round answer, not a crash.
    assert cli._round_boundary_may_upgrade(fleet, argparse.Namespace()) is True


def test_the_cli_asks_no_scheduler_at_one_lane(tmp_path):
    """The acceptance criterion made structural: at `lanes = 1` there is no
    fleet to schedule, so `_fleet_plan` answers `None` and the selection below
    it is the one the existing continuous-mode tests pin."""
    one = make_config(tmp_path, lanes=1)
    two = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"))

    assert cli._fleet_plan(one, registry, set()) is None
    assert cli._fleet_plan(two, registry, set()) is not None


def test_one_lane_still_selects_with_an_upgrade_pending_and_a_full_queue(
    tmp_path, monkeypatch
):
    """The single-lane dispatch sequence, unchanged where it would be easiest to
    change it: a pending upgrade and more ready tasks than lanes. Today the
    session is opened and the ORCHESTRATOR reaches the boundary at its next
    READY phase; the drain must not intercept that, or a single-lane loop would
    sleep next to a merged upgrade it can never reach."""
    config = upgrade_config(tmp_path, lanes=1)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1"), a_task("t2")))
    monkeypatch.setattr(
        cli,
        "_self_upgrade_at_boundary",
        lambda *a, **k: pytest.fail("the drain must not fire at one lane"),
    )
    selected: list = []

    def select(cfg, store, registry):
        selected.append(registry.next_ready().id)
        raise StopTheLoop()

    monkeypatch.setattr(cli, "_select_and_kickoff", select)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)

    assert selected == ["t1"]
    assert "fleet_hold" not in transcript_types(config), (
        "a single-lane loop has no fleet to report on"
    )


# ---- 6. and the dispatch site is bound to the same plan -----------------------
#
# The other end of admission control. `cli._fleet_plan` decides whether a lane
# OPENS a session; without this half, a lane that opened because SOMETHING was
# admissible could then be directed at a task the very same plan held for a
# scope conflict — two lanes each authorized to write the file the other one is
# editing, which is the case the gate exists for. Everything below drives
# `Orchestrator._dispatch_executor` itself, because that is where a directive
# becomes a round.


def no_conversation():  # pragma: no cover - reached only by a regression
    raise AssertionError("an admission decision opens no conversation")


def build_lane(
    config: AutoloopConfig, tasks=(), lane_index: int = 0
) -> tuple[Orchestrator, list[str]]:
    """One lane's orchestrator over a real state dir, mid-round, with the
    produce-then-review path doubled.

    `_dispatch_task_postcommit` is where a worker repo, a commit and an attempt
    come from, so REACHING it is the whole of "this was dispatched" — and
    running it would need a git repository, a real executor and an agent, none
    of which this claim is about. Its state file is written at a non-terminal
    phase, so this lane reads as BUSY to any supervisor that looks: the gate
    must exclude the slot this lane already holds, or every dispatch above one
    lane would read as a full fleet.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(lane_state_file(config.state_dir, lane_index))
    state = LoopState(session_id=f"lane-{lane_index}", conversation_url=URL)
    state.phase = Phase.EXECUTING.value
    store.save(state)
    registry = registry_of(*tasks)
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_conversation,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        lane_index=lane_index,
    )
    dispatched: list[str] = []
    orch._dispatch_task_postcommit = lambda d, t, s: dispatched.append(t.id)
    return orch, dispatched


def implement(task_id: str) -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="next", task_id=task_id)


def a_busy_lane(config: AutoloopConfig, lane_index: int, task_id: str) -> None:
    """A neighbour lane mid-round on `task_id`, written as that lane's own state
    file — the only thing the supervisor reads about a lane it is not in."""
    state = LoopState(session_id=f"lane-{lane_index}", conversation_url=URL)
    state.phase = Phase.EXECUTING.value
    state.current_task = {"task_id": task_id}
    StateStore(lane_state_file(config.state_dir, lane_index)).save(state)


def test_a_lane_may_not_dispatch_the_task_the_plan_held(tmp_path):
    """THE binding. Lane 1 is running n1, which declares `autoloop/cli.py`; the
    reviewer directs this lane at t2, which declares the same file. The dispatch
    is refused and the task is left EXACTLY as it was — pending, unwritten and
    unattempted — while t3, which the same plan admits, dispatches from the same
    session one directive later."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(
        config,
        tasks=[
            a_task("n1", ["autoloop/cli.py"]),
            a_task("t2", ["autoloop/cli.py"]),
            a_task("t3", ["autoloop/health.py"]),
        ],
    )
    orch._registry.mark_in_progress("n1")
    orch._task_store.save(orch._registry)
    a_busy_lane(config, 1, "n1")
    before = config.tasks_file.read_bytes()

    orch._dispatch_executor(implement("t2"))

    assert dispatched == [], "the held task did not start"
    assert orch._registry.get("t2").status == "pending"
    assert config.tasks_file.read_bytes() == before, "the registry was not written"
    assert not config.executions_dir.exists(), "no attempt was charged"
    assert orch.state.policy_denials == 1
    assert denial_codes(config) == [FLEET_HOLD_DENIAL_CODE]
    outbox = orch.state.outbox or ""
    assert HOLD_SCOPE_CONFLICT in outbox and "autoloop/cli.py" in outbox
    assert "'t3' instead" in outbox, "the correction names a task that CAN start"
    assert Phase(orch.state.phase) is Phase.READY, "corrected, never parked"

    orch._dispatch_executor(implement("t3"))

    assert dispatched == ["t3"], "an admitted task dispatches from the same session"
    assert orch._registry.get("t3").status == "in_progress"


def test_the_gate_measures_the_fleet_and_not_the_queue_position(tmp_path):
    """A directive may name any READY task, not only the head of the queue —
    policy has always authorized that. So the gate asks about the task the
    directive NAMES (`plan(first=...)`): with one lane free and three tasks
    ready, the plan's own scheduling answer for t2 is `HOLD_AT_CAP`, because t1
    was admitted into the last slot ahead of it. Reading that as a refusal would
    deny a legal directive and spend the denial budget on the queue's ordering.
    """
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(
        config,
        tasks=[
            a_task("n1", ["autoloop/n.py"]),
            a_task("t1", ["autoloop/a.py"]),
            a_task("t2", ["autoloop/b.py"]),
            a_task("t3", ["autoloop/c.py"]),
        ],
    )
    orch._registry.mark_in_progress("n1")
    orch._task_store.save(orch._registry)
    a_busy_lane(config, 1, "n1")
    scheduled = FleetSupervisor(2).plan(orch._registry, [busy(1, "n1")])
    assert admitted_ids(scheduled) == ["t1"] and scheduled.hold_reason("t2") == HOLD_AT_CAP

    orch._dispatch_executor(implement("t2"))

    assert dispatched == ["t2"], "held by position is not held by the fleet"
    assert orch.state.policy_denials == 0


def test_a_revise_of_the_arc_this_lane_already_owns_is_not_an_admission(tmp_path):
    """The failure a membership test against `admitted` alone would ship. A
    `revise` continues work this lane was already admitted for, and the ONE
    instant it is not covered by "in progress, therefore not in the queue" is
    the race the plan documents: the session names the task while its registry
    row still reads `pending`. Refusing there would refuse every revise in that
    window, the reviewer would re-send it, and the lane would fault-stop on an
    exhausted denial budget — with a conflicting neighbour, which is when it
    happens, the refusal would be permanent."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(
        config,
        tasks=[a_task("n1", ["autoloop/cli.py"]), a_task("t1", ["autoloop/cli.py"])],
    )
    orch._registry.mark_in_progress("n1")
    orch._task_store.save(orch._registry)
    a_busy_lane(config, 1, "n1")
    # The session owns t1; the registry row has not caught up yet.
    orch.state.current_task = {"task_id": "t1", "decision": "implement"}
    assert orch._registry.get("t1").status == "pending"

    orch._dispatch_executor(
        Directive(
            decision=Decision.REVISE,
            reason="tighten the claim",
            task_id="t1",
            feedback="one test is asserting the fixture",
        )
    )

    assert dispatched == ["t1"], "the arc this lane owns continues"
    assert orch.state.policy_denials == 0


def test_at_one_lane_the_dispatch_site_consults_no_fleet_at_all(tmp_path, monkeypatch):
    """The acceptance criterion, at the second call site. At `lanes = 1` the gate
    returns before it reads anything — no lane state file, no upgrade record —
    so the dispatch sequence is the one the existing tests pin, and a fleet the
    gate could not have read cannot change it."""
    config = make_config(tmp_path, lanes=1)
    orch, dispatched = build_lane(config, tasks=[a_task("t1", ["autoloop/cli.py"])])
    monkeypatch.setattr(
        orchestrator,
        "lane_occupants",
        lambda *a, **k: pytest.fail("a single-lane loop asked the fleet a question"),
    )

    orch._dispatch_executor(implement("t1"))

    assert dispatched == ["t1"]
    assert orch.state.policy_denials == 0


def test_a_fleet_the_gate_cannot_read_refuses_rather_than_dispatching(
    tmp_path, monkeypatch
):
    """FAIL-CLOSED, the direction this whole candidate is graded on. An
    unreadable neighbour is already a hold inside `plan`; this is the outer
    version — the computation itself raising — and it must refuse rather than
    dispatch blind, because a check that passes when what it needs is missing is
    not a check. The failure is named in the transcript, so a fleet that starts
    refusing everything says why."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(config, tasks=[a_task("t1", ["autoloop/cli.py"])])

    def unreadable(*_args, **_kwargs):
        raise OSError("the state directory went away")

    monkeypatch.setattr(orchestrator, "lane_occupants", unreadable)

    orch._dispatch_executor(implement("t1"))

    assert dispatched == []
    assert orch._registry.get("t1").status == "pending"
    assert orch.state.policy_denials == 1
    assert HOLD_LANE_UNREADABLE in (orch.state.outbox or "")
    assert "fleet_admission_unreadable" in transcript_types(config)


def test_a_drain_stops_a_dispatch_an_open_session_could_otherwise_start(tmp_path):
    """The drain, at the site `cli._fleet_plan` cannot reach: a session that was
    already open when the upgrade landed. Nothing new starts in it, and the
    correction asks for the one answer that helps — `stop` frees this lane, and
    an empty fleet is what the boundary is waiting for.

    The second half is the bound: a sha this run has already answered stops
    draining, exactly as it does in `cli._drainable_upgrade_sha`, so a boundary
    that could not hand off does not wedge the fleet shut."""
    config = upgrade_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(config, tasks=[a_task("t1", ["autoloop/cli.py"])])

    orch._dispatch_executor(implement("t1"))

    assert dispatched == []
    assert orch._registry.get("t1").status == "pending"
    assert not config.executions_dir.exists(), "no attempt was charged"
    outbox = orch.state.outbox or ""
    assert HOLD_DRAINING in outbox
    assert "Nothing else may start in this lane" in outbox

    assert orch.decline_self_upgrade("b" * 40) is True

    orch._dispatch_executor(implement("t1"))

    assert dispatched == ["t1"], "an answered upgrade stops draining the fleet"


def test_a_corrupt_upgrade_record_does_not_wedge_every_dispatch(tmp_path):
    """TWO fail directions, held at once on two different files, and getting
    them the same way round would be the bug. An unreadable FLEET refuses,
    because a lane that might hold anything cannot be admitted beside. An
    unreadable UPGRADE marker does NOT: `UpgradeStore.load` answers `None` for
    it — the direction `cli._drainable_upgrade_sha` and `_self_upgrade_due` both
    take, since the merged code is on disk either way — so a corrupt bookkeeping
    file cannot hold every lane's dispatch shut."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(config, tasks=[a_task("t1", ["autoloop/cli.py"])])
    config.pending_upgrade_file.write_text("{ not json", encoding="utf-8")

    assert orch._fleet_drain_pending() is False

    orch._dispatch_executor(implement("t1"))

    assert dispatched == ["t1"]
    assert orch.state.policy_denials == 0


def test_the_correction_names_no_alternative_the_urgent_pin_would_refuse(tmp_path):
    """Two gates, one directive, and they must not send the reviewer in a
    circle. While an operator's pin is live, `_refused_ahead_of_urgent` refuses
    every id but that one — so a fleet hold on the pinned task must not answer
    "send t3 instead", which the gate above would refuse in the very next
    round."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(
        config,
        tasks=[
            a_task("n1", ["autoloop/cli.py"]),
            a_task("t2", ["autoloop/cli.py"]),
            a_task("t3", ["autoloop/health.py"]),
        ],
    )
    orch._registry.mark_in_progress("n1")
    orch._task_store.save(orch._registry)
    a_busy_lane(config, 1, "n1")
    orch._registry.request_urgent("t2", "production is down")

    orch._dispatch_executor(implement("t2"))

    assert dispatched == [], "the pin does not overrule the conflict"
    outbox = orch.state.outbox or ""
    assert "Nothing else may start in this lane" in outbox
    assert "t3" not in outbox, "a correction the urgent gate refuses is not offered"


def test_a_directive_naming_a_task_the_plan_never_scheduled_is_left_to_policy(
    tmp_path,
):
    """The gate answers about ADMISSIONS and about nothing else. A task that is
    not READY was never scheduled by this plan — it has no hold reason — so the
    refusal belongs to `policy._check_task_reference` and to
    `_dispatch_task_postcommit`, which answer it for reasons of their own. An
    efficiency gate that invented a second, weaker copy of a correctness check
    would be the worse of the two answers."""
    config = make_config(tmp_path, lanes=2)
    orch, dispatched = build_lane(
        config, tasks=[a_task("n1", ["autoloop/cli.py"]), a_task("t1", ["autoloop/cli.py"])]
    )
    orch._registry.mark_in_progress("n1")
    orch._registry.mark_in_progress("t1")
    orch._task_store.save(orch._registry)
    a_busy_lane(config, 1, "n1")

    plan = FleetSupervisor(2).plan(orch._registry, [busy(1, "n1")], first="t1")
    assert plan.hold_reason("t1") == "", "an in-progress task is not in the queue"

    orch._dispatch_executor(implement("t1"))

    assert dispatched == ["t1"]
    assert orch.state.policy_denials == 0
