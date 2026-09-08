"""TURNING CONCURRENCY ON — candidate 9 of docs/AUTOLOOP.md's split plan
(conc-10).

**THE CLAIM: N tasks are implemented concurrently, each producing an
independently reviewable candidate, and the candidates reach the base one at a
time with none stranded.**

Four sections, and each is one part of it:

1. **Two lanes really run at once.** The evidence is a `threading.Barrier`
   inside the stub round: lane 0 cannot leave its implementation until lane 1
   has entered its own, so a runner that ran the lanes one after another fails
   this by TIMING OUT rather than by an assertion somebody wrote. The spans are
   recorded either way and asserted to intersect, so the failure message tells
   "they ran sequentially" apart from "one lane never started". What the round
   leaves behind is the independently reviewable half: two execution records,
   two candidate shas, two lane state files at the two paths conc-05 defined,
   and lane 1's own `lane_index` on the orchestrator that lane built.
2. **The self-upgrade boundary belongs to ONE lane.** THE defect this candidate
   was recut for: a non-owner lane could reach the drain, fail to hand off, and
   decline the pending sha for the whole run — after which the designated owner
   never sees the upgrade again and the merged code sits on disk with nothing
   able to act on it. A non-owner now takes no part: it neither calls
   `_reach_upgrade_boundary` nor writes `answered_upgrades`, and the owner
   arriving afterwards still finds the record pending and unanswered — on BOTH
   doors, the drain's and the round's own `SELF_UPGRADE` outcome, each driven
   in the order that produced the defect. **One
   lane, and never NO lane:** ownership follows the lowest lane still running,
   because a fleet does not restart a lane that ended and pinning it to lane 0
   left a fleet that lost lane 0 draining for an upgrade nothing could take.
3. **Merges stay one at a time under real concurrency.** conc-08 pins the merge
   token against a token file a test wrote; what only a fleet can show is two
   sweeps racing for it, which is why this one uses two threads. Exactly one
   merges; the other defers and merges on its next sweep, with nothing stolen.
4. **A lane that ends does not end the fleet**, and one that stops for the
   handoff is restarted when the replacement does not happen.
5. **The two things N lanes SHARE, and both were lost updates.** `tasks.json` is
   one file every lane holds a registry of for a whole round, so a lane saving
   its own transition used to write a stale copy of every other row back over a
   neighbour's — a completion, a quarantine or an `in_progress` claim, silently
   undone, and a task then readable as dispatchable in two lanes at once. And
   `publisher.git` is one repository every lane publishes through, where two
   simultaneous fetches collide on git's own lock and the loser is a lane parked
   for nothing but its neighbour's timing. Section 5 pins the registry half —
   deterministically, with no threads, because the claim is about what a save
   writes — and section 6 the publisher half, with two threads, because the claim
   there is that the second one WAITS.
6. **And a third: `pending_upgrade.json`.** One record, saved by the lane whose
   merge changed `autoloop/` and cleared by every lane's second iteration — so
   unserialised, a confirmation reads `execed`, a sibling's merge writes
   `pending` over it, and the confirmation unlinks an upgrade nobody has
   answered. Section 7 pins the store's compare-and-clear directly, then drives
   the window itself.

DELIBERATELY NOT RE-TESTED HERE, because the mechanism is another candidate's
and a second copy is a cost every round pays: the merge window's per-candidate
obligation and the carry-forward + re-review (conc-03,
`test_merge_rereview.py`), the token's own recovery and its `lanes = 1`
silence (conc-08, `test_lane_death_recovery.py`), the supervisor's cap,
admission and drain (conc-06, `test_fleet_supervisor.py`), and that the shipped
template still says `lanes = 1` (conc-02, `test_config_concurrency.py`).

No git repository, no subprocess and no agent: the CLAIM here is about the
loop's own arrangement of lanes, and the round that a lane runs is stubbed at
`cli._build_orchestrator` — the same seam `test_self_upgrade.py` substitutes at.
Threads are the one expensive thing, and they are the point: sections 1 and 3
are exactly the claims a single-threaded test cannot fail for the right reason.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import pytest

from autoloop import auto_merge, cli
from autoloop.auto_merge import (
    UPGRADE_EXEC_FAILED,
    UPGRADE_EXECED,
    UPGRADE_PENDING,
    PendingUpgrade,
    UpgradeStore,
)
from autoloop.config import (
    AutoloopConfig,
    BrowserConfig,
    ConcurrencyConfig,
    lane_id,
)
from autoloop.merge_sweep import (
    DEFERRED,
    BacklogSweeper,
    SweepCandidate,
    merge_token_file,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    LANES_DIRNAME,
    LoopState,
    Phase,
    StateStore,
    lane_paths,
    utcnow_iso,
)
from autoloop.tasks import (
    CO_SCHEDULE_EXEMPT_PATHS,
    Task,
    TaskRegistry,
    TaskStore,
    mutex_path_for,
)
from autoloop.worktask import TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/conc-10"

#: What a pending self-upgrade names as the tree it was merged into. Every test
#: here doubles the boundary itself, so applicability is never reached and this
#: must not be this process's own checkout.
A_REPO_ROOT = "/not/a/real/checkout"

#: How long a lane will wait at the barrier for its neighbour. Generous, because
#: it is only ever reached when the claim is FALSE — two lanes that really do
#: overlap pass through it immediately.
OVERLAP_TIMEOUT = 30.0


def make_config(
    tmp_path: Path, lanes: int = 2, auto_merge_enabled: bool = False
) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(auto_merge_enabled=auto_merge_enabled),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers",
        concurrency=ConcurrencyConfig(lanes=lanes),
    )


def a_task(task_id: str) -> Task:
    """A task whose scope is the one entry an overlap in is forgiven, so the
    supervisor's answer here is about the CAP and never a scope conflict."""
    return Task(
        id=task_id,
        title=f"task {task_id}",
        description="a task a lane of the fleet implements",
        approved_paths=tuple(CO_SCHEDULE_EXEMPT_PATHS),
    )


def registry_of(*tasks: Task) -> TaskRegistry:
    registry = TaskRegistry()
    registry.add_many(list(tasks))
    return registry


def continuous_args() -> argparse.Namespace:
    return argparse.Namespace(config=None, continuous=True, null_executor=False)


def an_upgrade(base_sha: str, status: str = UPGRADE_PENDING) -> PendingUpgrade:
    """One record of the shape `auto_merge._note_loop_code_merge` writes, with
    the two fields every bound in this design is keyed on named by the caller."""
    return PendingUpgrade(
        base_sha=base_sha,
        previous_base_sha="a" * 40,
        candidate_sha="c" * 40,
        task_id="conc-10b",
        repo_root=A_REPO_ROOT,
        paths=["autoloop/cli.py"],
        status=status,
        recorded_at=utcnow_iso(),
    )


def upgrade_config(tmp_path: Path, lanes: int = 2) -> AutoloopConfig:
    config = make_config(tmp_path, lanes=lanes)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    UpgradeStore(config.pending_upgrade_file).save(
        PendingUpgrade(
            base_sha="b" * 40,
            previous_base_sha="a" * 40,
            candidate_sha="c" * 40,
            task_id="conc-10",
            repo_root=A_REPO_ROOT,
            paths=["autoloop/cli.py"],
            status=UPGRADE_PENDING,
            recorded_at=utcnow_iso(),
        )
    )
    return config


def entries(config: AutoloopConfig, entry_type: str) -> list[dict]:
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [row.get("data") or {} for row in rows if row.get("type") == entry_type]


class StopTheLoop(Exception):
    """Ends `_run_continuous` from inside a fake poll — reaching it at all is
    half of the assertion."""


# =============================================================================
# 1. two lanes, at the same time
# =============================================================================


class Overlapping:
    """The stub round every lane of the fleet runs, standing in for the
    orchestrator `cli._build_orchestrator` would have built.

    It does the three things the claim is about and nothing else: it marks its
    lane BUSY (so the supervisor's occupancy scan is reading real state files),
    it waits at a shared barrier — which is the whole of "at the same time" —
    and it records an execution record with a candidate sha, which is what makes
    the round independently reviewable.
    """

    #: Set by the one test that uses this, before the fleet is opened. Declared
    #: here so a reader sees what the class carries rather than inferring it.
    barrier: threading.Barrier
    spans: list = []
    built: list = []
    timed_out: list = []

    def __init__(self, config, args, store, state, task_store, registry, lane_index=0):
        self.config = config
        self.store = store
        self.state = state
        self.lane_index = lane_index
        Overlapping.built.append(lane_index)

    def decline_self_upgrade(self, base_sha):    # pragma: no cover - never offered
        return True

    def run(self, max_steps=None):
        task_id = (self.state.current_task or {})["task_id"]
        self.state.phase = Phase.EXECUTING.value
        self.store.save(self.state)
        started = time.monotonic()
        try:
            Overlapping.barrier.wait(timeout=OVERLAP_TIMEOUT)
        except threading.BrokenBarrierError:
            Overlapping.timed_out.append(self.lane_index)
        TaskExecutionStore(self.config.executions_dir).save(
            TaskExecution(
                task_id=task_id,
                task_branch=f"autoloop/{task_id}",
                worktree_path=str(self.config.workers_root / task_id),
                task_base_sha="0" * 40,
                candidate_sha=f"{self.lane_index}" * 40,
            )
        )
        Overlapping.spans.append((self.lane_index, task_id, started, time.monotonic()))
        self.state.phase = Phase.STOPPED.value
        self.store.save(self.state)
        return Phase.STOPPED.value


def test_two_lanes_implement_two_tasks_at_the_same_time(tmp_path, monkeypatch):
    """THE claim's first half, and the one round 1 of this candidate was
    rejected for asserting rather than showing.

    The barrier is the evidence: a lane cannot leave its implementation until
    the other has entered its own, so a `_run_fleet` that ran the lanes in
    sequence would sit at `OVERLAP_TIMEOUT` and then fail on `timed_out` —
    which is a different failure message from "one lane never started", because
    the spans are recorded either way and asserted separately.

    What is left behind afterwards is the reviewable half: two execution
    records with two different candidate shas, one per task, and two lane state
    files at the two paths Decision 2 defines — lane 0's LITERALLY `state.json`.
    """
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1"), a_task("t2")))
    Overlapping.barrier = threading.Barrier(2)
    Overlapping.spans = []
    Overlapping.built = []
    Overlapping.timed_out = []
    queue = ["t1", "t2"]
    handing_out = threading.Lock()

    def kickoff(cfg, store, registry):
        """`_select_and_kickoff`'s stub: open a session in THIS lane's own
        store, or pause the fleet once the queue is empty — which is how both
        lanes reach a clean end instead of polling forever."""
        with handing_out:
            task_id = queue.pop(0) if queue else ""
        if not task_id:
            cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.pause_file.write_text("done\n", encoding="utf-8")
            return False
        state = LoopState.new(URL)
        state.current_task = {"task_id": task_id, "title": f"task {task_id}"}
        store.save(state)
        return True

    monkeypatch.setattr(cli, "_select_and_kickoff", kickoff)
    monkeypatch.setattr(cli, "_build_orchestrator", Overlapping)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    lane_zero = cli._LaneEntry(config)
    with lane_zero:
        code = cli._run_fleet(continuous_args(), config, None, lane_zero)

    assert code == 0, "both lanes ended cleanly"
    assert Overlapping.timed_out == [], (
        "a lane waited out the barrier — the lanes did not overlap"
    )
    assert len(Overlapping.spans) == 2, (
        f"only these lanes ran an implementation: {Overlapping.spans}"
    )
    lanes = sorted(span[0] for span in Overlapping.spans)
    assert lanes == [0, 1], "the fleet ran one round per lane, in its own lane"
    assert sorted(span[1] for span in Overlapping.spans) == ["t1", "t2"]
    latest_start = max(span[2] for span in Overlapping.spans)
    earliest_end = min(span[3] for span in Overlapping.spans)
    assert latest_start <= earliest_end, (
        f"the two implementations did not overlap in time: {Overlapping.spans}"
    )
    # The orchestrator each lane built was told which lane it was — the value
    # that points a round at its own clone and its own sibling set (conc-04).
    assert sorted(Overlapping.built) == [0, 1]
    # Two candidates, independently reviewable: one record per task, each with
    # its own sha, written by the lane that produced it.
    records = TaskExecutionStore(config.executions_dir)
    shas = {task_id: records.load(task_id).candidate_sha for task_id in ("t1", "t2")}
    assert len(set(shas.values())) == 2, f"one candidate served two tasks: {shas}"
    # And the two lanes' state files are the two paths Decision 2 names.
    assert (config.state_dir / "state.json").exists(), "lane 0 writes state.json"
    assert lane_paths(config.state_dir, 1).state_file.exists()
    assert lane_paths(config.state_dir, 1).state_file.parent.name == lane_id(1)
    # Lane 1's lease is gone: it was released by unwinding out of its own entry.
    assert not lane_paths(config.state_dir, 1).lease_file.exists()


def test_a_lane_that_raises_leaves_a_record_and_not_a_silent_short_fleet(
    tmp_path, monkeypatch
):
    """The fail-open a thread introduces and a single-lane loop never had: an
    exception out of `_run_continuous` used to reach the operator as a
    traceback, and out of a thread it reaches nobody — the fleet would simply
    run one lane short with nothing said. It is recorded, the other lane keeps
    working, and the fleet exits 2."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry())
    reached: list[int] = []
    # Both lanes are inside their first tick before either moves, so lane 0's
    # pause cannot end lane 1 before it has failed — the ordering the claim is
    # about, pinned rather than hoped for.
    both_here = threading.Barrier(2)

    def explode(cfg, blockers, lane):
        index = 0 if lane is None else lane.lane_index
        reached.append(index)
        both_here.wait(timeout=OVERLAP_TIMEOUT)
        if index == 1:
            raise RuntimeError("lane 1 fell over")
        # Lane 0 has nothing to do and would poll forever; one pause ends it
        # after this tick, which is the ordinary way a `run` stops. Its next
        # iteration returns at `pause_requested`, above this function.
        cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.pause_file.write_text("stop\n", encoding="utf-8")
        return None

    monkeypatch.setattr(cli, "_fleet_stop_reached", explode)
    monkeypatch.setattr(cli, "_select_and_kickoff", lambda *a, **k: False)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    lane_zero = cli._LaneEntry(config)
    with lane_zero:
        code = cli._run_fleet(continuous_args(), config, None, lane_zero)

    assert sorted(set(reached)) == [0, 1], "both lanes were opened"
    assert code == 2, "a lane that raised is a fleet that exits 2"
    failed = entries(config, "fleet_lane_failed")
    assert [row["lane_index"] for row in failed] == [1]
    assert failed[0]["lane_id"] == lane_id(1)
    assert "RuntimeError" in failed[0]["error"]


# =============================================================================
# 2. the self-upgrade boundary belongs to ONE lane
# =============================================================================


def test_a_non_owner_lane_leaves_a_pending_upgrade_unanswered_for_the_owner(
    tmp_path, monkeypatch
):
    """THE DEFECT this candidate was recut for, driven in the order that
    produced it: the NON-OWNER meets the boundary FIRST.

    `FleetPlan.upgrade_boundary` is a fact about the FLEET — an upgrade is
    pending and every lane is idle — so every lane sees it on the same tick.
    Without an owner the first lane to arrive answers for all of them, and a
    boundary it cannot hand off from declines the sha into `answered_upgrades`;
    `_drainable_upgrade_sha` then answers `""`, the drain stops, and the
    designated owner is never offered the upgrade again for the rest of the run.

    So three things are asserted about lane 1's tick, and the third is the one a
    "did not exec" assertion would miss: it did not reach the boundary, it wrote
    nothing into the fleet's answered set, and it did not fall through into a
    SESSION either — `upgrade_boundary` is `draining and fleet_idle`, so the
    hold below the boundary is where a refused lane lands.

    Then the owner arrives on the same record and the whole chain still works:
    it asks the fleet to stop (the replacement belongs to the runner, because
    `os.execv` keeps the pid and no sibling lease may be on disk), and the
    boundary `_run_fleet` takes next is handed the sha that was still pending.
    """
    config = upgrade_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry())
    fleet = cli._FleetRun(2)
    lane_one = cli._LaneEntry(config, 1, fleet)
    monkeypatch.setattr(
        cli,
        "_self_upgrade_at_boundary",
        lambda *a, **k: pytest.fail("a non-owner lane reached the boundary"),
    )
    monkeypatch.setattr(
        cli,
        "_select_and_kickoff",
        lambda *a, **k: pytest.fail("a drain must admit nothing"),
    )

    def poll(seconds):
        if seconds == cli.CONTINUOUS_POLL_SECONDS:
            raise StopTheLoop()   # the hold is where the refused lane landed

    monkeypatch.setattr(cli.time, "sleep", poll)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config, None, lane_one)

    assert fleet.answered_upgrades == set(), (
        "a non-owner lane consumed the fleet's pending upgrade"
    )
    assert cli._drainable_upgrade_sha(config, fleet.answered_upgrades) == "b" * 40, (
        "the owner would no longer drain for the upgrade"
    )
    assert UpgradeStore(config.pending_upgrade_file).load().status == UPGRADE_PENDING
    held = entries(config, "fleet_hold")
    assert held and held[0]["draining"] is True, "the refused lane held, not selected"
    assert not fleet.handoff_wanted, "a non-owner does not stop the fleet either"

    # ---- and now the owner, on the record the non-owner left alone ----------
    boundaries: list[str] = []

    def reached(cfg, lock, args=None, lane=None):
        boundaries.append(
            UpgradeStore(cfg.pending_upgrade_file).load().base_sha
        )
        return UPGRADE_EXEC_FAILED

    monkeypatch.setattr(cli, "_self_upgrade_at_boundary", reached)
    lane_zero = cli._LaneEntry(config, 0, fleet)

    code = cli._run_continuous(continuous_args(), config, None, lane_zero)

    assert code == 0 and fleet.handoff_wanted, "the owner asked the fleet to stop"
    assert fleet.stopped_for_handoff() == (0,)
    assert boundaries == [], "the lane thread must not exec — the runner does"

    # What `_run_fleet` does next, with every lane out of the way.
    cli._reach_upgrade_boundary(
        config, None, continuous_args(), lane_zero, fleet.answered_upgrades
    )

    assert boundaries == ["b" * 40], "the owner reached the boundary the non-owner left"
    assert "b" * 40 in fleet.answered_upgrades, "and the run is bounded against it"


def test_a_non_owner_declines_a_round_boundary_into_its_own_set(tmp_path, monkeypatch):
    """The OTHER door to the boundary — `Orchestrator.run` answering
    `SELF_UPGRADE` — and the same rule.

    It is unreachable in a fleet today, because `_round_boundary_may_upgrade`
    switches the per-round boundary off for a continuous run above one lane. It
    is gated anyway: "unreachable" is a property of another function, and this
    is the branch that would otherwise let a non-owner answer for the fleet.

    A refused lane still has to bound ITSELF — nothing settles the record, so
    the very next round would be offered the same sha forever — and the bound
    goes into a set only that lane reads. The two are asserted together, because
    a refusal that wrote to `answered_upgrades` would look identical from the
    lane's own side and would be the defect above wearing another name.

    THEN THE OWNER ARRIVES, in the second half below, and that is the ordering
    the defect was reported in: the non-owner meets this door FIRST and leaves
    the sha unanswered, and lane 0 — reaching the SAME door afterwards, on the
    same record — still finds it pending and still acts on it. One test rather
    than two, because the order is the claim: a fresh fixture would prove the
    owner can take a boundary nobody refused, which was never in doubt.
    """
    config = upgrade_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry())
    StateStore(lane_paths(config.state_dir, 1).state_file).save(
        _ready_session("lane-one")
    )
    fleet = cli._FleetRun(2)
    lane_one = cli._LaneEntry(config, 1, fleet)
    rounds: list[int] = []
    lanes_told: list[int] = []

    class OffersAnUpgrade:
        def __init__(self, state):
            self.state = state
            self.declined: list[str] = []

        def decline_self_upgrade(self, base_sha):
            self.declined.append(base_sha)
            return True

        def run(self, max_steps=None):
            rounds.append(len(rounds))
            if len(rounds) > 2:
                raise StopTheLoop()   # two rebuilds is all the bound needs
            return cli.SELF_UPGRADE

    built: list[OffersAnUpgrade] = []

    def build(config_, args_, store_, state_, task_store_, registry_, **kwargs):
        lanes_told.append(kwargs.get("lane_index", 0))
        built.append(OffersAnUpgrade(state_))
        return built[-1]

    monkeypatch.setattr(cli, "_build_orchestrator", build)
    monkeypatch.setattr(
        cli,
        "_self_upgrade_at_boundary",
        lambda *a, **k: pytest.fail("a non-owner lane reached the boundary"),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "_select_and_kickoff", lambda *a, **k: False)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config, None, lane_one)

    assert fleet.answered_upgrades == set(), "the fleet's set is not a non-owner's"
    assert UpgradeStore(config.pending_upgrade_file).load().status == UPGRADE_PENDING
    assert cli._drainable_upgrade_sha(config, fleet.answered_upgrades) == "b" * 40
    refusals = entries(config, "self_upgrade_not_this_lane")
    assert refusals and refusals[0]["base_sha"] == "b" * 40
    assert refusals[0]["lane_id"] == lane_id(1)
    assert refusals[0]["owner_lane_id"] == lane_id(0)
    # The bound, read off the two instances that show it separately. The FIRST
    # declines only what the refusal itself declined; the THIRD never reached a
    # refusal at all (its `run` ends the loop), so the one decline on it can
    # only have come from the carried `lane_declined` — which is the claim.
    assert built[0].declined == ["b" * 40], "the refusal declines on its instance"
    assert built[2].declined == ["b" * 40], (
        "a rebuilt orchestrator is not handed the sha this lane already refused, "
        "so the same boundary is offered again at the speed of a `continue`"
    )
    # And the round this lane ran was told which lane it belongs to.
    assert lanes_told == [1, 1, 1]

    # ---- and now the OWNER, on the record the non-owner left pending --------
    # The ordering half of the claim, asked of THIS door rather than the
    # drain's: the non-owner met `SELF_UPGRADE` FIRST, so the owner arriving
    # afterwards has to find the sha still unanswered and still act on it.
    # Stated as what the owner CAN do rather than as what the non-owner did,
    # because that is the direction the defect was reported in — without the
    # gate the fleet's set already holds `b * 40` by this line and the boundary
    # below is never offered to the lane that owns it.
    StateStore(lane_paths(config.state_dir, 0).state_file).save(
        _ready_session("lane-zero")
    )
    boundaries: list[str] = []

    def reached(cfg, lock, args=None, lane=None):
        boundaries.append(UpgradeStore(cfg.pending_upgrade_file).load().base_sha)
        return UPGRADE_EXEC_FAILED

    monkeypatch.setattr(cli, "_self_upgrade_at_boundary", reached)
    # Exactly one boundary, counted rather than sampled: the double ends the
    # loop on its next call (`len(rounds) > 2`), so seeding one round leaves
    # the owner one `SELF_UPGRADE` to answer and one rebuild after it.
    rounds[:] = [0]

    with pytest.raises(StopTheLoop):
        cli._run_continuous(
            continuous_args(), config, None, cli._LaneEntry(config, 0, fleet)
        )

    assert boundaries == ["b" * 40], (
        "the owner did not reach the boundary the non-owner left pending"
    )
    assert built[-2].declined == [], (
        "the owner's round was handed a sha the non-owner had refused, so the "
        "boundary it owns would have been declined before it saw it"
    )
    assert fleet.answered_upgrades == {"b" * 40}, (
        "the owner's bound is the FLEET's — which is exactly what a "
        "non-owner's refusal above is not"
    )
    assert built[-1].declined == ["b" * 40], "and it rides the rebuild after it"
    assert lanes_told[-2:] == [0, 0], "the rounds the owner ran were lane 0's"
    assert not fleet.handoff_wanted, (
        "this door acts in place rather than asking the runner to empty the "
        "fleet, and it may: it is unreachable in a real fleet — "
        "`_round_boundary_may_upgrade` switches the per-round boundary off for "
        "a continuous run above one lane — while the DRAIN is the door a fleet "
        "actually arrives at, and that one hands off. The gate above it is "
        "there anyway because 'unreachable' is a property of another function. "
        "If this door is ever offered in a fleet, this line is what must "
        "change, and it must change to a handoff: `os.execv` keeps the pid, so "
        "a sibling lease still on disk would name the successor's own pid"
    )


def test_a_lane_that_is_the_whole_loop_still_owns_its_own_boundary(tmp_path):
    """The acceptance criterion, asked of the gate itself. The owner is lane 0,
    and a caller holding NO lane is the single-lane loop — both of the call
    shapes `_run_continuous` supports, and every existing boundary test, answer
    True, so nothing about a single-lane deployment moved."""
    config = make_config(tmp_path, lanes=1)

    assert cli._lane_owns_upgrade(None) is True
    assert cli._lane_owns_upgrade(cli._LaneEntry(config)) is True
    assert cli._lane_owns_upgrade(cli._LaneEntry(config, 0)) is True
    assert cli._lane_owns_upgrade(cli._LaneEntry(config, 1)) is False
    assert cli.UPGRADE_OWNER_LANE == 0
    # A lane with no fleet behind it takes the boundary itself rather than
    # asking a runner that does not exist — which is what keeps `_run_locked`'s
    # and the existing drain tests' call shapes working.
    assert cli._LaneEntry(config).fleet is None
    assert cli._LaneEntry(config).handoff_wanted is False


def test_the_fleet_restarts_the_lanes_when_the_replacement_does_not_happen(
    tmp_path, monkeypatch
):
    """A handoff that is refused must not cost the fleet.

    The three non-exec outcomes leave the record `pending` on purpose, so the
    process carries on with the code it has — and the lanes that stepped out of
    the way for the replacement have to come BACK, or a preflight failure would
    quietly reduce a fleet of two to nothing. The sha is bound into
    `answered_upgrades` on the way past, which is what stops the restarted fleet
    draining for the same record immediately.
    """
    config = upgrade_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry())
    refusals: list[int] = []
    #: `(how many boundaries had been attempted, which lane)` per tick. The
    #: first number is what tells a restarted pass from the first one, without
    #: counting ticks and hoping.
    ticks: list[tuple[int, int]] = []
    # A real, tiny wait rather than a no-op: the non-owner HOLDS on the drain
    # until the owner has asked, and a poll that returns instantly would spin
    # that wait into thousands of transcript lines while the other thread runs.
    real_sleep = time.sleep
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: real_sleep(0.01))
    monkeypatch.setattr(cli, "_select_and_kickoff", lambda *a, **k: False)

    def refused(*_args, **_kwargs):
        refusals.append(1)
        return UPGRADE_EXEC_FAILED

    monkeypatch.setattr(cli, "_self_upgrade_at_boundary", refused)
    # BOTH restarted lanes are observed before either pauses the fleet. Without
    # it the first lane back would write the pause and the second would return
    # at `pause_requested` before ticking at all — a restart that happened but
    # left no evidence, which is the assertion below failing for the wrong
    # reason.
    both_back = threading.Barrier(2)

    def observe(cfg, blockers, lane):
        ticks.append((len(refusals), 0 if lane is None else lane.lane_index))
        if refusals:
            both_back.wait(timeout=OVERLAP_TIMEOUT)
            # The restarted pass has been observed; end it the ordinary way.
            cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.pause_file.write_text("done\n", encoding="utf-8")
        return None

    monkeypatch.setattr(cli, "_fleet_stop_reached", observe)

    lane_zero = cli._LaneEntry(config)
    with lane_zero:
        code = cli._run_fleet(continuous_args(), config, None, lane_zero)

    assert code == 0
    assert len(refusals) == 1, "the boundary was reached exactly once"
    restarted = {index for attempts, index in ticks if attempts}
    assert restarted == {0, 1}, (
        f"the lanes were not both restarted after the refused handoff: {ticks}"
    )
    assert "b" * 40 in lane_zero.answered_upgrades, (
        "the restarted fleet would drain for the same record again"
    )
    assert UpgradeStore(config.pending_upgrade_file).load().status == UPGRADE_PENDING


def test_the_upgrade_owner_moves_to_the_lowest_lane_still_running(tmp_path):
    """THE PRICE OF PINNING OWNERSHIP TO LANE 0, and the residual conc-10 named
    rather than closed: `_run_fleet` does not restart a lane that ENDED, and
    thirteen codes are lane-fatal, so a lane-fatal park in lane 0 left the fleet
    running with NO upgrade owner in it. Every remaining lane then refuses every
    boundary as somebody else's, the drain never ends, and an upgrade merged
    after that point waits for an operator to restart the process.

    Ownership therefore follows the lowest lane still running. Asserted as a
    sequence rather than a single case, because the two properties that keep the
    original defect closed are properties of the SEQUENCE: exactly one lane
    answers True at every step, and the answer only ever moves UP — so no two
    lanes can both consume one pending upgrade, whichever order they tick in.
    """
    config = make_config(tmp_path, lanes=3)
    fleet = cli._FleetRun(3)
    lanes = [cli._LaneEntry(config, index, fleet) for index in range(3)]

    def owners():
        return [cli._lane_owns_upgrade(lane) for lane in lanes]

    assert owners() == [True, False, False], "a fresh fleet is lane 0's"
    fleet.begin_pass((0, 1, 2))
    assert owners() == [True, False, False], "and so is one that has just opened"

    fleet.lane_ended(0)
    assert owners() == [False, True, False], "lane 0 left; lane 1 owns it now"
    assert cli._upgrade_owner_index(lanes[2]) == 1, "and the refusal names it"
    fleet.lane_ended(1)
    assert owners() == [False, False, True]

    fleet.lane_ended(2)
    assert owners() == [False, False, False], (
        "a fleet with nothing running has no owner — the fail-closed direction, "
        "since nothing is left to act on a boundary in a fleet that is unwinding"
    )
    assert fleet.upgrade_owner() is None
    assert cli._upgrade_owner_index(lanes[0]) == cli.UPGRADE_OWNER_LANE, (
        "and with no owner the refusal names where ownership starts"
    )

    # A restarted pass opens only the lanes that stepped aside, and the lowest
    # of THOSE owns the boundary — not the lane an earlier pass had.
    fleet.begin_pass((1, 2))
    assert owners() == [False, True, False]


def test_a_fleet_that_lost_lane_zero_still_reaches_the_upgrade_boundary(
    tmp_path, monkeypatch
):
    """The same residual, driven through the real runner rather than the gate.

    Lane 0 falls out of the fleet on its first tick — a lane-fatal ending, which
    `_run_fleet` deliberately does not restart — and the upgrade that is pending
    the whole time must still be reached. Before this, every lane left was a
    non-owner: each one refused the boundary, `plan.draining` held it, and the
    fleet polled beside a merged tree until an operator restarted it.

    The wait on `lane.owns_upgrade` is what makes this deterministic rather than
    a race: lane 1's tick under test is taken AFTER lane 0 has really gone.
    """
    config = upgrade_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry())
    boundaries: list[str] = []
    real_sleep = time.sleep
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: real_sleep(0.01))
    monkeypatch.setattr(cli, "_select_and_kickoff", lambda *a, **k: False)

    def reached(cfg, lock, args=None, lane=None):
        boundaries.append(UpgradeStore(cfg.pending_upgrade_file).load().base_sha)
        return UPGRADE_EXEC_FAILED

    monkeypatch.setattr(cli, "_self_upgrade_at_boundary", reached)

    def observe(cfg, blockers, lane):
        index = 0 if lane is None else lane.lane_index
        if index == 0:
            raise RuntimeError("lane 0 fell out of the fleet")
        if boundaries:
            # The boundary has been taken; end the restarted pass the ordinary
            # way rather than polling forever.
            cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.pause_file.write_text("done\n", encoding="utf-8")
            return None
        deadline = time.monotonic() + OVERLAP_TIMEOUT
        while not lane.owns_upgrade and time.monotonic() < deadline:
            real_sleep(0.005)
        return None

    monkeypatch.setattr(cli, "_fleet_stop_reached", observe)

    lane_zero = cli._LaneEntry(config)
    with lane_zero:
        code = cli._run_fleet(continuous_args(), config, None, lane_zero)

    assert boundaries == ["b" * 40], (
        "the fleet never reached the boundary after losing lane 0 — the upgrade "
        "would sit on disk until an operator restarted the process"
    )
    assert entries(config, "self_upgrade_not_this_lane") == [], (
        "the surviving lane refused a boundary it now owns"
    )
    failed = entries(config, "fleet_lane_failed")
    assert [row["lane_index"] for row in failed] == [0], "lane 0 really did end"
    assert code == 2, "and its ending is still what the fleet exits on"
    assert "b" * 40 in lane_zero.answered_upgrades


def _ready_session(session_id: str) -> LoopState:
    state = LoopState(session_id=session_id, conversation_url=URL)
    state.phase = Phase.READY.value
    state.current_task = {"task_id": "t1", "title": "task t1"}
    return state


# =============================================================================
# 3. two lanes merging at once
# =============================================================================


class FakeGit:
    """The two reads `merge_sweep._probe` makes, and nothing else."""

    def __init__(self, head: str = "f" * 40):
        self.head = head

    def head_sha(self) -> str:
        return self.head

    def dirty_files(self):
        return []


class RacingSweeper(BacklogSweeper):
    """`BacklogSweeper` with the enumeration and the merge doubled, so what is
    under test is the token gate around them and nothing else — the same shape
    `test_lane_death_recovery.DoubledSweeper` uses, with the merge slowed enough
    that a second lane genuinely has to wait for it."""

    def __init__(self, hold: float = 0.05, **kwargs):
        super().__init__(**kwargs)
        self.attempted: list[str] = []
        self._hold = hold

    def _backlog(self, seen, result):
        return [
            SweepCandidate(
                task_id, "a" * 40, "origin", f"refs/heads/{task_id}", (1, 0.0, task_id)
            )
            for task_id in ("t-a", "t-b")
        ]

    def _attempt(self, candidate, seen):
        self.attempted.append(candidate.task_id)
        time.sleep(self._hold)
        return auto_merge.MERGED


def test_two_lanes_sweeping_at_once_merge_one_at_a_time(tmp_path, monkeypatch):
    """"The candidates reach the base one at a time" under real concurrency.

    conc-08 pins the merge token against a token file a test wrote, which is the
    right test for the RECOVERY. This is the one only a fleet can fail: two
    sweeps racing for the same token, where a check-then-act would let both
    through and a single-threaded test would never see it. Exactly one lane
    merges; the other defers, steals nothing, and merges on its next sweep.
    """
    config = make_config(tmp_path, lanes=2, auto_merge_enabled=True)
    monkeypatch.setattr(cli, "_merge_window_blockers", lambda *a, **k: ((), ()))
    sweepers = [
        RacingSweeper(
            config=config,
            git=FakeGit(),
            policy=PolicyEngine(config.policy),
            execution_store=TaskExecutionStore(config.executions_dir),
            registry=TaskRegistry([]),
            log=lambda *a, **k: None,
            lane_index=index,
        )
        for index in (0, 1)
    ]
    ready = threading.Barrier(2)
    results: dict[int, object] = {}

    def sweep(index):
        ready.wait(timeout=OVERLAP_TIMEOUT)
        results[index] = sweepers[index].sweep()

    threads = [threading.Thread(target=sweep, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=OVERLAP_TIMEOUT)

    merged = [index for index in (0, 1) if sweepers[index].attempted]
    assert len(merged) == 1, (
        f"both lanes merged at once: {[s.attempted for s in sweepers]}"
    )
    waiting = 1 - merged[0]
    assert results[waiting].outcome == DEFERRED
    assert "merge token" in results[waiting].reasons[0]
    assert not merge_token_file(config.state_dir).exists(), "the token went back"

    # and the lane that waited merges on its next sweep, with nothing stolen
    again = sweepers[waiting].sweep()

    assert again.merged == ["t-a", "t-b"]
    assert sweepers[waiting].attempted == ["t-a", "t-b"]
    assert not merge_token_file(config.state_dir).exists()


# =============================================================================
# 4. and at one lane, none of this exists
# =============================================================================


def test_one_lane_runs_in_the_calling_thread_and_builds_no_fleet(
    tmp_path, monkeypatch
):
    """The acceptance criterion made structural: at `lanes = 1` `_cmd_run` runs
    the one lane through `_run_continuous` in the calling thread, so no thread
    is started, no `_FleetRun` is built, no lease file exists and no `lanes/`
    directory appears. The thread NAME is the assertion, because "it ran here"
    is the one fact a fleet runner slipped in below one lane would change."""
    config = make_config(tmp_path, lanes=1)
    TaskStore(config.tasks_file).save(TaskRegistry())
    monkeypatch.setattr(cli, "_select_and_kickoff", lambda *a, **k: False)
    thread = threading.current_thread()
    seen: list[str] = []

    def poll(seconds):
        seen.append(threading.current_thread().name)
        raise StopTheLoop()

    monkeypatch.setattr(cli.time, "sleep", poll)

    lane = cli._LaneEntry(config)
    with lane:
        with pytest.raises(StopTheLoop):
            cli._run_continuous(continuous_args(), config, None, lane)

    assert seen == [thread.name], "the single lane ran somewhere other than here"
    assert lane.fleet is None and lane.answered_upgrades == set()
    assert not (config.state_dir / LANES_DIRNAME).exists()
    assert not lane_paths(config.state_dir, 0).lease_file.exists()


# =============================================================================
# 5. one task file, N lanes
# =============================================================================
#
# THE LOST UPDATE. Each lane loads `tasks.json` at the top of its outer
# iteration and holds that registry object for the whole round; every save
# writes the WHOLE registry back. So lane 0 recording its own completion also
# writes its hour-old copy of lane 1's row — and lane 1's dispatch, completion
# or quarantine is gone with no error anywhere. `TaskStore.save` reconciled
# `priority` and nothing else, because until there was a second LANE the only
# other writer was the operator's priority edit.
#
# No threads here on purpose: two lanes are two registries and two stores, which
# a single thread can hold at once, and the claim is about what the second save
# WRITES rather than about when it runs. The interleaving is written out step by
# step, so a failure names the step.


def two_lanes_holding(config: AutoloopConfig):
    """Two lanes' (store, registry) pairs, loaded from one file at the same
    instant — `cli._load_tasks` itself, so what is under test includes the
    wiring that decides whether a store reconciles at all."""
    return cli._load_tasks(config), cli._load_tasks(config)


def test_a_lane_cannot_overwrite_another_lanes_status_transition(tmp_path):
    """THE regression. Lane 1 dispatches t2; lane 0, whose registry still reads
    t2 as `pending`, then saves its own dispatch of t1 — and used to write t2
    back to `pending`, after which the supervisor reads t2 as READY and a second
    lane runs the task lane 1 is already implementing.

    Both directions are asserted, because a merge that simply preferred the disk
    would pass the first assertion and lose lane 0's own work in the second."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1"), a_task("t2")))
    (store_zero, lane_zero), (store_one, lane_one) = two_lanes_holding(config)
    assert store_zero.fleet and store_one.fleet, "above one lane a store reconciles"

    lane_one.mark_in_progress("t2")
    store_one.save(lane_one)
    lane_zero.mark_in_progress("t1")  # on a registry that predates the line above
    store_zero.save(lane_zero)

    on_disk = TaskStore(config.tasks_file).load()
    assert on_disk.get("t2").status == "in_progress", (
        "lane 1's dispatch was overwritten by lane 0's stale registry"
    )
    assert on_disk.get("t1").status == "in_progress", "lane 0's own write survived"
    assert lane_zero.get("t2").status == "in_progress", (
        "lane 0 is still holding the stale row it would write again next save"
    )

    # And the round continues. This is the half a one-shot merge gets wrong: the
    # row lane 0 ADOPTED is not lane 0's change, so its next save must not write
    # it back over whatever lane 1 did after that.
    lane_one.mark_completed("t2")
    store_one.save(lane_one)
    lane_zero.mark_completed("t1")
    store_zero.save(lane_zero)

    on_disk = TaskStore(config.tasks_file).load()
    assert on_disk.get("t2").status == "completed", "an adopted row was written back"
    assert on_disk.get("t1").status == "completed"


def test_at_one_lane_a_save_is_the_one_it_has_always_been(tmp_path):
    """The acceptance criterion, at the persistence layer: with `lanes = 1` no
    store reconciles anything but priority, so the very same interleaving ends
    exactly as it does today — last write wins. Asserted rather than assumed,
    because a reconciliation that quietly ran below two lanes would be a change
    to how every single-lane deployment's task file is written."""
    config = make_config(tmp_path, lanes=1)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1"), a_task("t2")))
    (store_zero, lane_zero), (store_other, other) = two_lanes_holding(config)
    assert not store_zero.fleet and not store_other.fleet

    other.mark_in_progress("t2")
    store_other.save(other)
    lane_zero.mark_in_progress("t1")
    store_zero.save(lane_zero)

    on_disk = TaskStore(config.tasks_file).load()
    assert on_disk.get("t2").status == "pending", "today's behaviour, unchanged"
    assert on_disk.get("t1").status == "in_progress"


def test_the_reconciliation_never_undoes_this_lanes_own_change(tmp_path):
    """The other direction, and the one that would make this cure worse than the
    disease: a row THIS registry changed is its own and is written, whatever the
    file says. Two lanes marking the same task is the race the dispatch claim
    refuses; if one ever gets past it, the writer's own decision stands rather
    than being silently replaced by the loser's."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1")))
    (store_zero, lane_zero), (store_one, lane_one) = two_lanes_holding(config)

    lane_one.block("t1", "a question for the operator")
    store_one.save(lane_one)
    lane_zero.mark_in_progress("t1")
    store_zero.save(lane_zero)

    assert TaskStore(config.tasks_file).load().get("t1").status == "in_progress"


def test_a_task_another_lane_added_is_not_dropped_by_a_stale_save(tmp_path):
    """A whole row, not a field. Lane 1's approved plan adds two subtasks; lane
    0's next ordinary save wrote a registry that had never heard of them, and the
    tasks vanished — a plan the reviewer approved, gone, with the parent left
    pointing at nothing."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1"), a_task("t2")))
    (store_zero, lane_zero), (store_one, lane_one) = two_lanes_holding(config)

    lane_one.add_many([a_task("t2a"), a_task("t2b")])
    store_one.save(lane_one)
    lane_zero.mark_in_progress("t1")
    store_zero.save(lane_zero)

    on_disk = TaskStore(config.tasks_file).load()
    assert sorted(task.id for task in on_disk.all_tasks()) == [
        "t1", "t2", "t2a", "t2b",
    ]
    assert lane_zero.has("t2a"), "and lane 0 can see the tasks it just wrote"


def test_a_task_file_nobody_can_parse_still_records_a_completion(tmp_path):
    """FAILS OPEN, deliberately and in the direction `reconcile_priorities`
    already chose: this method sits on the path that records completions and
    quarantines, so a save that started REFUSING because the bytes it is about to
    replace will not parse would be the worse failure of the two. The lane's own
    work lands; what is lost is a merge with a file that had nothing readable in
    it to merge."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(registry_of(a_task("t1")))
    store, registry = cli._load_tasks(config)
    config.tasks_file.write_text("{ not json at all", encoding="utf-8")

    registry.mark_in_progress("t1")
    store.save(registry)

    assert TaskStore(config.tasks_file).load().get("t1").status == "in_progress"


# =============================================================================
# 6. one publisher repository, N lanes
# =============================================================================
#
# `publisher.git` is ONE bare repository under the state directory and every
# lane publishes through it. `import_candidate` runs `git fetch`, which takes
# git's own `FETCH_HEAD` lock: a second lane arriving mid-fetch does not queue,
# it FAILS (`Unable to create '.../FETCH_HEAD.lock': File exists`) — and a lane
# parked on a push refusal for no reason but a neighbour's timing is the fleet
# breaking work that a single loop did fine.
#
# Threads here, for section 1's reason inverted: the claim is that the second
# lane WAITS, which nothing single-threaded can fail for the right reason.


class SerialisedGit:
    """A git runner that stands in for the publisher's repository, blocks the
    first FETCH inside it until the test lets go, and records whether two ever
    ran there at once."""

    def __init__(self, url: str, hooks: Path):
        self.url = url
        self.hooks = hooks
        self.entered = threading.Event()     # a fetch has begun
        self.second_entered = threading.Event()
        self.release = threading.Event()     # ...and may now finish
        self.fetched: list[str] = []
        self.inside = 0
        self.overlapped = False
        self.guard = threading.Lock()

    def __call__(self, args, cwd=None, capture_output=True, text=False, env=None):
        command = list(args[1:])
        if command[0] == "fetch":
            with self.guard:
                self.fetched.append(command[-1])
                self.inside += 1
                self.overlapped = self.overlapped or self.inside > 1
                if len(self.fetched) > 1:
                    self.second_entered.set()
            self.entered.set()
            self.release.wait(timeout=OVERLAP_TIMEOUT)
            with self.guard:
                self.inside -= 1
            return _proc("", text)
        if command[:2] == ["cat-file", "commit"]:
            # A commit object, as bytes: `read_commit` reads raw stdout.
            return _proc(f"tree {'e' * 40}\n\nreviewed\n", text)
        if command[:2] == ["rev-parse", "--git-path"]:
            return _proc(str(self.hooks), text)
        if command[:2] == ["config", "--get-all"]:
            return _proc(self.url, text)
        return _proc("", text)


def _proc(out: str, text: bool):
    class Done:
        returncode = 0
        stdout = out if text else out.encode("utf-8")
        stderr = "" if text else b""

    return Done()


def a_publisher(tmp_path: Path, runner: SerialisedGit):
    """A `Publisher` over `runner` — no git, no network. Construction runs the
    same structural checks it always does; the runner answers them."""
    from autoloop.publisher import Publisher

    return Publisher(
        tmp_path / ".al" / "publisher.git",
        "origin",
        PolicyEngine(PolicyConfig()),
        runner=runner,
    )


def test_two_lanes_publishing_at_once_take_the_repository_one_at_a_time(tmp_path):
    """THE claim: the second lane WAITS and then publishes. Not "the two calls
    happened not to overlap" — the first lane is held INSIDE its fetch until this
    test releases it, so without the mutex the second lane's fetch would start
    immediately and `second_entered` would be set. Both calls then succeed, which
    is the half that says the fix is not "one lane fails politely"."""
    runner = SerialisedGit(str(tmp_path / "remote.git"), tmp_path / "no-hooks")
    publisher = a_publisher(tmp_path, runner)
    done: dict[str, str] = {}
    failed: dict[str, BaseException] = {}

    def publish(name: str, sha: str):
        try:
            done[name] = publisher.import_candidate(tmp_path, sha)
        except BaseException as exc:  # noqa: BLE001 - reported, not raised
            failed[name] = exc

    first = threading.Thread(target=publish, args=("lane0", "a" * 40))
    first.start()
    assert runner.entered.wait(timeout=OVERLAP_TIMEOUT), "lane 0 never fetched"

    second = threading.Thread(target=publish, args=("lane1", "b" * 40))
    second.start()
    assert not runner.second_entered.wait(0.25), (
        "lane 1 entered the publisher repo while lane 0 was fetching in it"
    )

    runner.release.set()
    first.join(timeout=OVERLAP_TIMEOUT)
    second.join(timeout=OVERLAP_TIMEOUT)

    assert failed == {}, f"a simultaneous publish failed a lane: {failed}"
    assert done == {"lane0": "a" * 40, "lane1": "b" * 40}
    assert sorted(runner.fetched) == ["a" * 40, "b" * 40], "both lanes published"
    assert runner.overlapped is False


def test_a_publisher_lock_that_cannot_be_taken_is_an_ordinary_git_failure(tmp_path):
    """The bound on the wait, and the shape of losing it. `TaskStoreBusy` is a
    `StateError`: raised out of a push it would leave the lane by traceback,
    while every caller of `publish` already handles a git failure by parking
    `push_refused` with the reason. So the conversion is the claim — and the
    reason travels with it, because "the publisher lock" is what an operator
    needs to read."""
    from autoloop.errors import GitCommandError
    from autoloop.publisher import publisher_mutex, publisher_mutex_path

    repo = tmp_path / ".al" / "publisher.git"
    lock = publisher_mutex_path(tmp_path / ".al")
    assert lock.name == "publisher.git.lock", "beside the repository, never inside it"
    assert lock.parent == (tmp_path / ".al").resolve(), "and under the state dir"
    holding = threading.Event()
    let_go = threading.Event()

    def hold():
        with publisher_mutex(repo):
            holding.set()
            let_go.wait(timeout=OVERLAP_TIMEOUT)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=OVERLAP_TIMEOUT)
    try:
        with pytest.raises(GitCommandError) as caught:
            with publisher_mutex(repo, timeout=0.05):
                pytest.fail("a held publisher lock let a second lane in")
    finally:
        let_go.set()
        holder.join(timeout=OVERLAP_TIMEOUT)

    assert "publisher" in str(caught.value)
    assert "Nothing was fetched or pushed" in str(caught.value)


# =============================================================================
# 7. one pending-upgrade record, N lanes
# =============================================================================
#
# THE THIRD LOST UPDATE, and the one that loses an UPGRADE rather than a row.
# `pending_upgrade.json` is a single record: a lane whose merge changed
# `autoloop/` SAVES a fresh `pending` one, and every lane's second iteration
# reads it and CLEARS it if it says `execed` (`cli._confirm_self_upgrade`).
# Unserialised, those two interleave — the confirmation reads `execed`, a
# sibling's merge writes `pending` over it, the confirmation unlinks — and the
# new upgrade is gone with no boundary ever offered it and no entry saying so.
# That is the silent-no-outcome failure the self-upgrade path exists to end,
# rebuilt one function over.
#
# The store contract is pinned first, with no threads and no patching, because
# that is where the claim actually lives; the window itself is then driven
# deterministically, and the two-lane case with threads because "exactly one
# confirmation" is not a claim a single thread can fail for the right reason.


def test_the_upgrade_record_is_cleared_only_by_the_identity_that_read_it(tmp_path):
    """COMPARE-AND-CLEAR, asked of the store directly. Both halves of the
    identity are checked, and separately: a clear keyed on the sha alone would
    still delete a record whose STATUS moved on under it, and one keyed on the
    status alone would delete a newer record that happens to carry the same
    one — which is why the sha is compared even though the `pending` a sibling's
    merge writes already fails the status half."""
    config = make_config(tmp_path, lanes=2)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    store = UpgradeStore.for_config(config)
    store.save(an_upgrade("b" * 40, UPGRADE_EXECED))
    # ...and a sibling lane's merge lands, which is a DIFFERENT upgrade rather
    # than a retry of the first (`PendingUpgrade`'s own docstring).
    store.save(an_upgrade("d" * 40, UPGRADE_PENDING))
    before = config.pending_upgrade_file.read_bytes()

    assert store.clear(base_sha="b" * 40, status=UPGRADE_EXECED) is False
    assert store.clear(base_sha="d" * 40, status=UPGRADE_EXECED) is False
    assert store.clear(base_sha="b" * 40, status=UPGRADE_PENDING) is False
    assert config.pending_upgrade_file.read_bytes() == before, (
        "a refused clear rewrote the record it refused to remove"
    )

    assert store.clear(base_sha="d" * 40, status=UPGRADE_PENDING) is True
    assert not config.pending_upgrade_file.exists()
    assert store.clear(base_sha="d" * 40, status=UPGRADE_PENDING) is False, (
        "a record that is already gone was not removed by this call either"
    )

    # And the unconditional form is untouched: it is what an operator-facing
    # caller means by "remove this file whatever it says".
    store.save(an_upgrade("e" * 40, UPGRADE_PENDING))
    assert store.clear() is True
    assert not config.pending_upgrade_file.exists()


def test_a_merge_that_lands_inside_the_confirmation_window_is_not_deleted(
    tmp_path, monkeypatch
):
    """THE RACE, made deterministic: the sibling's merge is injected into the
    exact window it used to be lost in — between the confirmation's read of the
    `execed` record and its removal of it.

    The injection fires on the FIRST load only, which is the one
    `_confirm_self_upgrade` makes outside any hold. The clear's own load happens
    inside the hold, and a test whose injection reached that one would be
    proving nothing about the window.
    """
    config = make_config(tmp_path, lanes=2)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    UpgradeStore(config.pending_upgrade_file).save(an_upgrade("b" * 40, UPGRADE_EXECED))
    real_load = UpgradeStore.load
    reads: list[int] = []

    def load_then_merge(self):
        record = real_load(self)
        reads.append(1)
        if len(reads) == 1:
            UpgradeStore(self.path).save(an_upgrade("d" * 40, UPGRADE_PENDING))
        return record

    monkeypatch.setattr(UpgradeStore, "load", load_then_merge)

    assert cli._confirm_self_upgrade(config) is False, (
        "a lane confirmed a replacement whose record it did not remove"
    )

    monkeypatch.undo()
    survivor = UpgradeStore(config.pending_upgrade_file).load()
    assert survivor is not None, "the sibling lane's upgrade was deleted unanswered"
    assert (survivor.base_sha, survivor.status) == ("d" * 40, UPGRADE_PENDING)
    assert cli._drainable_upgrade_sha(config, set()) == "d" * 40, (
        "and the fleet would still drain for it"
    )
    assert entries(config, "self_upgrade_confirmed") == [], (
        "a confirmation that removed nothing must not report a retirement"
    )
    skipped = entries(config, "self_upgrade_confirm_skipped")
    assert len(skipped) == 1 and skipped[0]["base_sha"] == "b" * 40, (
        "and it must not be silent either"
    )


def test_two_lanes_confirming_one_replacement_confirm_it_once(tmp_path):
    """One replacement is one `self_upgrade_confirmed` entry, however many lanes
    reach the top of their second iteration holding the same record.

    Both interleavings are the same answer, which is what makes this
    deterministic without pinning a schedule: a lane whose read came after the
    other's clear finds nothing to confirm, and one whose read came before it
    finds the record no longer its own."""
    config = make_config(tmp_path, lanes=2)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    UpgradeStore(config.pending_upgrade_file).save(an_upgrade("b" * 40, UPGRADE_EXECED))
    ready = threading.Barrier(2)
    answers: dict[int, bool] = {}

    def confirm(index: int):
        ready.wait(timeout=OVERLAP_TIMEOUT)
        answers[index] = cli._confirm_self_upgrade(config)

    threads = [threading.Thread(target=confirm, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=OVERLAP_TIMEOUT)

    assert sorted(answers.values()) == [False, True], f"both lanes: {answers}"
    assert len(entries(config, "self_upgrade_confirmed")) == 1
    assert not config.pending_upgrade_file.exists(), "the marker was retired once"


def test_at_one_lane_the_upgrade_record_is_written_as_it_always_was(tmp_path):
    """THE ACCEPTANCE CRITERION at this store: below two lanes nothing is
    serialised, so no mutex file appears beside the record and a single-lane
    state directory holds exactly what it holds today. The fleet store is
    asserted in the same test, because "the gate is real" and "the gate is off"
    are the same claim read from its two sides."""
    one = make_config(tmp_path / "one", lanes=1)
    one.state_dir.mkdir(parents=True, exist_ok=True)
    store = UpgradeStore.for_config(one)
    assert store.fleet is False
    store.save(an_upgrade("b" * 40, UPGRADE_EXECED))

    assert cli._confirm_self_upgrade(one) is True, "and the confirmation still fires"
    assert [p.name for p in one.state_dir.iterdir() if "pending_upgrade" in p.name] == []

    two = make_config(tmp_path / "two", lanes=2)
    two.state_dir.mkdir(parents=True, exist_ok=True)
    fleet_store = UpgradeStore.for_config(two)
    assert fleet_store.fleet is True
    fleet_store.save(an_upgrade("b" * 40, UPGRADE_EXECED))

    assert mutex_path_for(two.pending_upgrade_file).exists(), (
        "the fleet's writes are not going through the mutex at all"
    )
