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
   arriving afterwards still finds the record pending and unanswered.
3. **Merges stay one at a time under real concurrency.** conc-08 pins the merge
   token against a token file a test wrote; what only a fleet can show is two
   sweeps racing for it, which is why this one uses two threads. Exactly one
   merges; the other defers and merges on its next sweep, with nothing stolen.
4. **A lane that ends does not end the fleet**, and one that stops for the
   handoff is restarted when the replacement does not happen.

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
from autoloop.tasks import CO_SCHEDULE_EXEMPT_PATHS, Task, TaskRegistry, TaskStore
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
