"""Lane death and recovery — conc-08.

Candidate 7 of the nine in docs/AUTOLOOP.md, "Running several tasks at once —
the split plan", Decision 8. One claim:

    A lane that dies mid-round is recovered WITHOUT TOUCHING THE OTHERS.

Five sections, and the first is the claim itself:

1. **A lane killed mid-executing is resumed or quarantined on the next tick,
   and every other lane's state file, lease, clone and worker repository is
   byte-identical before and after.** Both halves are asserted, and the second
   half is why: a recovery that did NOTHING would pass a byte-identical
   assertion trivially, so each of these tests carries a positive control — the
   dead lane's lease really is gone, its worker really did move — beside the
   equality.
2. **The merge token.** A live lane's sweep DEFERS while a dead lane holds it
   (it is never stolen), the recovery releases it, and the same sweep then
   merges. At `lanes = 1` no token exists and no file is created.
3. **An unclean clone parks that lane and no other**, through the park that
   already exists (`observed_checkout_unusable`, lane-fatal since conc-07) —
   with the lane's own clone directory named in the question, and `fleet_stop`
   answering `None` for the sibling.
4. **Everything the recovery REFUSES to touch.** A lease nobody can read, a
   state file nobody can read, an execution record nobody can read, a lanes
   directory nobody can list, a live lease, a quarantine that fails — every one
   of them leaves the lane exactly as it was, lease included, so nothing enters
   it. And the case that looks like a death and is not: a mid-round state file
   with NO lease, which is what an ordinary `run` leaves between rounds.
5. **At `lanes = 1` none of it is reached**, and `health` says nothing about
   lanes — the acceptance criterion every candidate in the split carries.

Real git repositories are used where the claim is about git — a worker repo that
passes `worker_repo_is_reusable`, one that does not, and an observed clone with
residue in it — and nowhere else. The other lanes' CLONES are plain directories:
the recovery runs no git in a tree it does not own, so what is being asserted
about them is that their bytes do not move, which a directory shows as well as a
repository and 12ms cheaper.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autoloop import auto_merge, cli
from autoloop import lock as lock_module
from autoloop.blockers import FLEET_FATAL, LANE_FATAL, BlockerStore, fatal_scope
from autoloop.config import (
    AutoloopConfig,
    BrowserConfig,
    ConcurrencyConfig,
    lane_id,
    lane_observed_checkout,
)
from autoloop.git_gateway import GitGateway
from autoloop.health import check as health_check
from autoloop.health import dead_lane_survey, dead_lane_view
from autoloop.lock import LaneLease
from autoloop.manifest import ManifestStore
from autoloop.merge_sweep import (
    DEFERRED,
    MERGE_TOKEN_FILENAME,
    SWEPT,
    BacklogSweeper,
    SweepCandidate,
    merge_token_file,
)
from autoloop.orchestrator import (
    RECOVERY_QUARANTINED,
    RECOVERY_REFUSED,
    RECOVERY_RELEASED,
    RECOVERY_RESUMED,
    Orchestrator,
    fleet_stop,
    recover_dead_lanes,
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
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import (
    ObservedCheckout,
    WorkerRepoManager,
    worker_repo_is_reusable,
)
from autoloop.worktask import TaskExecution, TaskExecutionStore
from gitrepo import make_repo_from_template

URL = "https://chatgpt.com/c/lane-death-recovery"


# =============================================================================
# helpers
# =============================================================================


def make_config(tmp_path: Path, lanes: int = 3, auto_merge_enabled: bool = False):
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(auto_merge_enabled=auto_merge_enabled),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers",
        concurrency=ConcurrencyConfig(lanes=lanes),
    )


def a_task(task_id: str = "t1") -> Task:
    return Task(
        id=task_id,
        title=f"task {task_id}",
        description="a task a lane was working on when it died",
        approved_paths=("autoloop/tests/",),
    )


def pin_boot(monkeypatch) -> datetime:
    """Pin boot to an hour ago, so a record stamped two hours ago is DEAD
    however its pid probes.

    Every dead lease and dead token below carries `os.getpid()` — unquestionably
    alive — so these tests only pass if the boot ordering inside
    `LoopLock.is_live` is what decided, which is the same discipline
    `test_lane_state.py` states for its own dead leases.
    """
    boot = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: boot.timestamp())
    return boot


def iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def write_lease(config, index: int, *, alive: bool, boot=None, payload=None):
    """Put a lease at lane `index`. `payload` writes it verbatim (which is how
    the unreadable cases get to be unreadable)."""
    lease = LaneLease(config.state_dir, index)
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        lease.path.write_text(text, encoding="utf-8")
        return lease
    record = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": utcnow_iso() if alive else iso(boot - timedelta(hours=2)),
        "run_id": f"run-{index}",
        "lane_id": lane_id(index),
        "state_dir": str(lane_paths(config.state_dir, index).state_dir),
    }
    lease.path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return lease


def write_token(config, *, alive: bool, boot=None, payload=None, lane: int = 1):
    path = merge_token_file(config.state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        return path
    record = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": utcnow_iso() if alive else iso(boot - timedelta(hours=2)),
        "run_id": "run-token",
        "lane_id": lane_id(lane),
        "state_dir": str(config.state_dir),
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def seed_lane(config, index: int, *, phase=Phase.EXECUTING, execution=None):
    state = LoopState(session_id=f"lane-{index}", conversation_url=URL)
    state.phase = phase.value if isinstance(phase, Phase) else phase
    if execution is not None:
        state.task_execution = asdict(execution)
    StateStore(lane_paths(config.state_dir, index).state_file).save(state)
    return state


def seed_worker(config, task_id: str, *, real_repo: bool) -> tuple[Path, TaskExecution]:
    """A worker repository for `task_id` and the execution record naming it.

    `real_repo=True` builds one that passes `worker_repo_is_reusable`;
    `False` builds a directory that is not a git repository at all — the shape a
    half-finished `create()` or a killed clone leaves behind.
    """
    manager = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
    path = manager.path_for(task_id)
    branch = f"autoloop/{task_id}"
    if real_repo:
        make_repo_from_template(
            path, branch=branch, files=(("work.txt", f"{task_id} work\n"),)
        )
    else:
        path.mkdir(parents=True)
        (path / "half-written.txt").write_text("what the dead round wrote\n")
    execution = TaskExecution(
        task_id=task_id,
        task_branch=branch,
        worktree_path=str(path),
        task_base_sha="0" * 40,
    )
    TaskExecutionStore(config.executions_dir).save(execution)
    return path, execution


def seed_clone(config, index: int) -> Path:
    """A stand-in for lane `index`'s observed clone at exactly the path
    `config.lane_observed_checkout` resolves it to."""
    root = Path(config.state_dir).parent / "observed"
    path = Path(lane_observed_checkout(root, index, config.concurrency.lanes))
    path.mkdir(parents=True, exist_ok=True)
    (path / "clone.txt").write_text(f"lane {index}'s tree\n")
    return path


def digest(path: Path) -> str:
    """A content digest of one file or one whole directory. `absent` is an
    answer, so a file that appears or disappears fails the comparison too."""
    if not path.exists():
        return "absent"
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    running = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        running.update(child.relative_to(path).as_posix().encode("utf-8"))
        running.update(b"\0")
        if child.is_file() and not child.is_symlink():
            running.update(child.read_bytes())
        running.update(b"\1")
    return running.hexdigest()


def lane_snapshot(config, index: int, worker: Path | None = None) -> dict:
    """Everything one lane owns on disk, digested: its state file, its lease,
    its clone and its worker repository. The four things Decision 8 says a
    recovery touches — asked about a lane the recovery must NOT have touched."""
    paths = lane_paths(config.state_dir, index)
    root = Path(config.state_dir).parent / "observed"
    snapshot = {
        "state": digest(paths.state_file),
        "lease": digest(paths.lease_file),
        "clone": digest(
            Path(lane_observed_checkout(root, index, config.concurrency.lanes))
        ),
    }
    if worker is not None:
        snapshot["worker"] = digest(worker)
    return snapshot


def build_orchestrator(config, lane_index: int, *, git=None, observed=None):
    """A collaborator-free Orchestrator in one lane — the same helper shape
    `test_fault_isolation.build_orchestrator` uses, plus the two collaborators
    the observed-clone park needs."""
    store = StateStore(lane_paths(config.state_dir, lane_index).state_file)
    state = LoopState.new(URL)
    store.save(state)
    registry = TaskRegistry([a_task("t1")])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    blockers = BlockerStore(config.blockers_dir)
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=None,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        blocker_store=blockers,
        observed_checkout=observed,
        lane_index=lane_index,
    )
    return orch, blockers


def transcript_entries(config, entry_type: str) -> list[dict]:
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [row.get("data") or {} for row in rows if row.get("type") == entry_type]


class FakeGit:
    """The two reads `merge_sweep._probe` makes, and nothing else."""

    def __init__(self, head: str = "f" * 40):
        self.head = head

    def head_sha(self) -> str:
        return self.head

    def dirty_files(self):
        return []


class DoubledSweeper(BacklogSweeper):
    """`BacklogSweeper` with the enumeration and the merge doubled, so the only
    thing under test is the token gate around them."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.attempted: list[str] = []

    def _backlog(self, seen, result):
        return [
            SweepCandidate(task_id, "a" * 40, "origin", f"refs/heads/{task_id}", (1, 0.0, task_id))
            for task_id in ("t-a", "t-b")
        ]

    def _attempt(self, candidate, seen):
        self.attempted.append(candidate.task_id)
        return auto_merge.MERGED


def make_sweeper(config, monkeypatch, lane_index: int = 0) -> DoubledSweeper:
    monkeypatch.setattr(cli, "_merge_window_blockers", lambda *a, **k: ((), ()))
    return DoubledSweeper(
        config=config,
        git=FakeGit(),
        policy=PolicyEngine(config.policy),
        execution_store=TaskExecutionStore(config.executions_dir),
        registry=TaskRegistry([]),
        log=lambda *a, **k: None,
        lane_index=lane_index,
    )


# =============================================================================
# 1. a lane killed mid-executing, and only that lane
# =============================================================================


def test_a_lane_killed_mid_executing_is_resumed_and_no_other_lane_moves(
    tmp_path, monkeypatch
):
    """THE claim, in its resume half. Lane 1 died mid-executing on a worker that
    is still exactly what its record says, so the round is resumed: its lease is
    removed — which is the only thing standing between the next tick and that
    lane — and nothing else about it is reset.

    Lane 2 is alive and mid-round beside it. Its state file, its lease, its
    clone and its worker repository are digested before and after, and the
    positive control is asserted with them: an equality on its own would pass
    just as happily if the recovery had done nothing at all.
    """
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=3)
    workers = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
    executions = TaskExecutionStore(config.executions_dir)

    dead_worker, dead_exec = seed_worker(config, "t-dead", real_repo=True)
    seed_lane(config, 1, execution=dead_exec)
    write_lease(config, 1, alive=False, boot=boot)
    seed_clone(config, 1)

    live_worker, live_exec = seed_worker(config, "t-live", real_repo=True)
    seed_lane(config, 2, execution=live_exec)
    write_lease(config, 2, alive=True, boot=boot)
    seed_clone(config, 2)

    before_live = lane_snapshot(config, 2, live_worker)
    before_dead_state = digest(lane_paths(config.state_dir, 1).state_file)

    recovery = recover_dead_lanes(
        config, exclude=0, worker_repos=workers, execution_store=executions
    )

    # the recovery
    assert [(e.lane_id, e.action) for e in recovery.lanes] == [
        (lane_id(1), RECOVERY_RESUMED)
    ]
    assert recovery.lanes[0].task_id == "t-dead"
    # ...actually happened: the lane is enterable again and its round survived
    assert not lane_paths(config.state_dir, 1).lease_file.exists()
    assert worker_repo_is_reusable(dead_worker, "autoloop/t-dead")
    assert digest(lane_paths(config.state_dir, 1).state_file) == before_dead_state
    assert (
        StateStore(lane_paths(config.state_dir, 1).state_file).load().phase
        == Phase.EXECUTING.value
    )
    # ...and cost the live lane nothing at all
    assert lane_snapshot(config, 2, live_worker) == before_live
    assert LaneLease(config.state_dir, 2).read() is not None


def test_a_dead_lane_whose_worker_fails_the_reuse_probe_is_quarantined(
    tmp_path, monkeypatch
):
    """THE claim, in its quarantine half. The worker at the recorded path is not
    a git repository, so it is MOVED ASIDE — never deleted, and no longer
    reachable by a later `create()` for that task id — before the lane is opened
    again. The sibling lane is byte-identical across it."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=3)
    workers = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)

    broken, broken_exec = seed_worker(config, "t-broken", real_repo=False)
    assert not worker_repo_is_reusable(broken, "autoloop/t-broken")
    seed_lane(config, 1, execution=broken_exec)
    write_lease(config, 1, alive=False, boot=boot)
    seed_clone(config, 1)

    live_worker, live_exec = seed_worker(config, "t-live", real_repo=True)
    seed_lane(config, 2, execution=live_exec)
    write_lease(config, 2, alive=True, boot=boot)
    seed_clone(config, 2)
    before_live = lane_snapshot(config, 2, live_worker)

    recovery = recover_dead_lanes(config, exclude=0, worker_repos=workers)

    entry = recovery.lanes[0]
    assert [e.lane_id for e in recovery.lanes] == [lane_id(1)]
    assert entry.action == RECOVERY_QUARANTINED and entry.task_id == "t-broken"
    moved = Path(entry.quarantined_at)
    assert moved.is_dir() and "quarantine" in moved.parts
    assert (moved / "half-written.txt").read_text() == "what the dead round wrote\n"
    assert not broken.exists()          # a later create() for t-broken is free
    assert not lane_paths(config.state_dir, 1).lease_file.exists()
    assert lane_snapshot(config, 2, live_worker) == before_live


def test_the_orchestrator_recovers_on_its_next_tick_and_records_what_it_did(
    tmp_path, monkeypatch
):
    """The production path: `Orchestrator.run` recovers before it takes a step,
    excluding its own lane, and every outcome reaches the transcript. A lane
    recovered silently is a round that vanished."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    _worker, execution = seed_worker(config, "t-dead", real_repo=True)
    seed_lane(config, 1, execution=execution)
    write_lease(config, 1, alive=False, boot=boot)
    orch, _blockers = build_orchestrator(config, lane_index=0)

    orch._recover_dead_lanes()

    assert not lane_paths(config.state_dir, 1).lease_file.exists()
    recorded = transcript_entries(config, "lane_recovered")
    assert [(r["lane_id"], r["action"]) for r in recorded] == [
        (lane_id(1), RECOVERY_RESUMED)
    ]
    assert recorded[0]["task_id"] == "t-dead" and recorded[0]["detail"]


# =============================================================================
# 2. the merge token
# =============================================================================


def test_a_dead_lanes_merge_token_is_released_and_a_live_lane_then_merges(
    tmp_path, monkeypatch
):
    """Decision 8's second bullet, end to end. While the dead lane's token is
    on disk the live lane's sweep DEFERS and attempts nothing — the token is
    never stolen, because "read it, judge it dead, overwrite it" is how two
    lanes come to merge at once. The recovery releases it from the fleet-lock
    holder, and the very same sweeper then merges."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2, auto_merge_enabled=True)
    write_token(config, alive=False, boot=boot, lane=1)
    sweeper = make_sweeper(config, monkeypatch, lane_index=0)

    deferred = sweeper.sweep()

    assert deferred.outcome == DEFERRED
    assert "merge token" in deferred.reasons[0]
    assert sweeper.attempted == []
    assert merge_token_file(config.state_dir).exists()   # not stolen

    recovery = recover_dead_lanes(config, exclude=0)

    assert recovery.merge_token == RECOVERY_RELEASED
    assert lane_id(1) in recovery.merge_token_detail
    assert not merge_token_file(config.state_dir).exists()

    swept = sweeper.sweep()

    assert swept.outcome == SWEPT and swept.merged == ["t-a", "t-b"]
    assert sweeper.attempted == ["t-a", "t-b"]
    # and the token went back on the way out, so the next lane may merge
    assert not merge_token_file(config.state_dir).exists()


def test_a_live_lanes_merge_token_is_never_released(tmp_path, monkeypatch):
    """The other direction, and the one that matters more: a token whose holder
    is alive is a lane that is merging right now. Removing it would be the
    concurrent merge the token exists to prevent."""
    pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2, auto_merge_enabled=True)
    write_token(config, alive=True, lane=1)
    before = digest(merge_token_file(config.state_dir))

    recovery = recover_dead_lanes(config, exclude=0)

    assert recovery.merge_token == ""
    assert digest(merge_token_file(config.state_dir)) == before


def test_an_unreadable_merge_token_defers_the_sweep_and_is_refused(
    tmp_path, monkeypatch
):
    """A token nobody can parse is not a free one. The sweep defers rather than
    merging on the strength of bytes nobody could read, and the recovery refuses
    to remove it rather than handing a shared resource to somebody else."""
    pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2, auto_merge_enabled=True)
    write_token(config, alive=False, payload="{not json at all")
    sweeper = make_sweeper(config, monkeypatch)

    result = sweeper.sweep()
    recovery = recover_dead_lanes(config, exclude=0)

    assert result.outcome == DEFERRED and sweeper.attempted == []
    assert recovery.merge_token == RECOVERY_REFUSED
    assert merge_token_file(config.state_dir).exists()


def test_one_lane_takes_no_merge_token_and_writes_no_file(tmp_path, monkeypatch):
    """THE acceptance criterion, in this module: at `lanes = 1` the sweep is
    what it has always been and no new file appears under the state dir."""
    config = make_config(tmp_path, lanes=1, auto_merge_enabled=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    sweeper = make_sweeper(config, monkeypatch)

    result = sweeper.sweep()

    assert result.outcome == SWEPT and sweeper.attempted == ["t-a", "t-b"]
    assert not merge_token_file(config.state_dir).exists()
    assert MERGE_TOKEN_FILENAME not in {p.name for p in config.state_dir.iterdir()}


def test_the_token_is_taken_around_the_merges_and_given_back_after_them(
    tmp_path, monkeypatch
):
    """The token is held FOR the merges and released on the way out — including
    the way out through an exception, which is what stops one crashed sweep
    wedging the fleet until a lane dies and is recovered."""
    pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2, auto_merge_enabled=True)
    sweeper = make_sweeper(config, monkeypatch)
    held: list[bool] = []

    def observe(candidate, seen):
        held.append(merge_token_file(config.state_dir).exists())
        if candidate.task_id == "t-b":
            raise RuntimeError("the merge machinery fell over")
        return auto_merge.MERGED

    sweeper._attempt = observe
    try:
        sweeper.sweep()
    except RuntimeError:
        pass

    assert held == [True, True]                       # held across every merge
    assert not merge_token_file(config.state_dir).exists()   # and given back


# =============================================================================
# 3. an unclean clone parks that lane and no other
# =============================================================================


def test_an_unclean_clone_parks_that_lane_and_no_other(tmp_path):
    """Decision 8's third bullet. `ObservedCheckout.synchronize` already refuses
    a clone that is not a tree only the loop has written to; under the fleet
    that refusal is a LANE-fatal park naming that lane's own directory, and the
    other lanes keep running.

    Nothing here is new code — that is the point of the test. What it pins is
    that the refusal, the park's code and conc-07's classification still line up,
    so a clone one lane over cannot stop the fleet.
    """
    config = make_config(tmp_path, lanes=2)
    primary = make_repo_from_template(tmp_path / "primary", branch="main")
    observed_root = tmp_path / "observed"
    lane_clone = Path(lane_observed_checkout(observed_root, 1, 2))
    make_repo_from_template(lane_clone, branch="main")
    (lane_clone / "stray.txt").write_text("something else wrote here\n")

    orch, blockers = build_orchestrator(
        config,
        lane_index=1,
        git=GitGateway(primary, PolicyEngine(config.policy)),
        observed=ObservedCheckout(observed_root),
    )

    proceeded = orch._synchronise_observed_checkout(a_task("t1"))

    assert proceeded is False
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"          # the axis is unchanged
    assert str(lane_clone) in orch.state.question        # it names ITS directory
    assert "stray.txt" in orch.state.question
    parked = blockers.open_blockers()
    assert [b.code for b in parked] == ["observed_checkout_unusable"]
    assert [b.lane_id for b in parked] == [lane_id(1)]
    # ...and the fleet keeps working: this stops one lane, not four.
    assert fatal_scope("observed_checkout_unusable") == LANE_FATAL
    assert fleet_stop(config, blockers, exclude=0) is None
    # the sibling's own clone was never even created, let alone written to
    assert not Path(lane_observed_checkout(observed_root, 0, 2)).exists()


def test_a_fleet_fatal_park_in_the_same_place_still_stops_everything(tmp_path):
    """The control for the test above: the isolation comes from the CODE, not
    from the park being in a lane. A code on the other side of
    `blockers.fatal_scope` reaches every lane from the same site."""
    config = make_config(tmp_path, lanes=2)
    orch, blockers = build_orchestrator(config, lane_index=1)

    orch._to_needs_user(
        "an agent wrote outside its worker repository",
        kind="loop_fatal",
        code="checkout_escape_detected",
    )

    assert fatal_scope("checkout_escape_detected") == FLEET_FATAL
    stop = fleet_stop(config, blockers, exclude=0)
    assert stop is not None and stop.lane_id == lane_id(1)


# =============================================================================
# 4. what the recovery refuses to touch
# =============================================================================


def test_a_mid_round_lane_with_no_lease_is_not_a_death(tmp_path, monkeypatch):
    """The case a recovery keyed on the STATE FILE would get wrong. `run`
    without `--continuous` finishes a round and returns with the session
    mid-flight, and `_LaneEntry` releases the lease on the way out — so a
    mid-round state file with no lease beside it is an ordinary boundary, not a
    death. Recovering it would quarantine a worker an operator is about to
    resume."""
    pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    worker, execution = seed_worker(config, "t-broken", real_repo=False)
    seed_lane(config, 1, execution=execution)
    before = lane_snapshot(config, 1, worker)

    recovery = recover_dead_lanes(config, exclude=0)

    assert recovery.lanes == ()
    assert lane_snapshot(config, 1, worker) == before


def test_a_live_lease_is_never_recovered(tmp_path, monkeypatch):
    """A lane with somebody in it is not a lane to recover, however long its
    round has been running: liveness is the lease's, and the lease is live."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    worker, execution = seed_worker(config, "t-broken", real_repo=False)
    seed_lane(config, 1, execution=execution)
    write_lease(config, 1, alive=True, boot=boot)
    before = lane_snapshot(config, 1, worker)

    recovery = recover_dead_lanes(config, exclude=0)

    assert recovery.lanes == ()
    assert lane_snapshot(config, 1, worker) == before


def test_an_unreadable_lease_is_refused_and_left_exactly_where_it_is(
    tmp_path, monkeypatch
):
    """A lease nobody can read is NOT a free lane. Removing what cannot be read
    is how a lane with a live process in it gets opened, so the lease stays, the
    worker is not touched, and the refusal is what an operator acts on."""
    pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    worker, execution = seed_worker(config, "t-broken", real_repo=False)
    seed_lane(config, 1, execution=execution)
    write_lease(config, 1, alive=False, payload="")   # the killed-mid-create shape
    before = lane_snapshot(config, 1, worker)

    recovery = recover_dead_lanes(config, exclude=0)

    assert [(e.lane_id, e.action) for e in recovery.lanes] == [
        (lane_id(1), RECOVERY_REFUSED)
    ]
    assert recovery.lanes[0].detail
    assert lane_snapshot(config, 1, worker) == before


def test_a_dead_lease_beside_an_unreadable_state_is_refused(tmp_path, monkeypatch):
    """A lane whose state cannot be read might hold anything — including a round
    whose worker must not be reused. Refusing keeps the lane shut."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    worker, _execution = seed_worker(config, "t-broken", real_repo=False)
    paths = lane_paths(config.state_dir, 1)
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text("{ this is not a state file", encoding="utf-8")
    write_lease(config, 1, alive=False, boot=boot)
    before = lane_snapshot(config, 1, worker)

    recovery = recover_dead_lanes(config, exclude=0)

    assert [e.action for e in recovery.lanes] == [RECOVERY_REFUSED]
    assert lane_snapshot(config, 1, worker) == before


def test_an_unreadable_execution_record_refuses_rather_than_quarantining(
    tmp_path, monkeypatch
):
    """The decision "resume or quarantine" is made FROM the execution record, so
    a record nobody can read is a decision nobody can make. Refusing leaves the
    worker where it is and the lane shut; quarantining on no evidence would move
    a repository an operator has to go and find."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    worker, execution = seed_worker(config, "t-dead", real_repo=True)
    seed_lane(config, 1, execution=execution)
    (config.executions_dir / "t-dead.json").write_text("{ torn", encoding="utf-8")
    write_lease(config, 1, alive=False, boot=boot)
    before = lane_snapshot(config, 1, worker)

    recovery = recover_dead_lanes(config, exclude=0)

    assert [e.action for e in recovery.lanes] == [RECOVERY_REFUSED]
    assert "could not be read" in recovery.lanes[0].detail
    assert lane_snapshot(config, 1, worker) == before


def test_a_quarantine_that_fails_leaves_the_lane_shut(tmp_path, monkeypatch):
    """The ORDER is the safety property: the worker is dealt with before the
    lease, so a quarantine that could not be performed must not end with the
    lane open onto a worker nobody made safe."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    worker, execution = seed_worker(config, "t-broken", real_repo=False)
    seed_lane(config, 1, execution=execution)
    write_lease(config, 1, alive=False, boot=boot)

    class RefusingManager(WorkerRepoManager):
        def quarantine(self, task_id, label):
            raise OSError("the quarantine destination is not writable")

    manager = RefusingManager(config.workers_root, config.worker_hooks_dir)
    recovery = recover_dead_lanes(config, exclude=0, worker_repos=manager)

    assert [e.action for e in recovery.lanes] == [RECOVERY_REFUSED]
    assert lane_paths(config.state_dir, 1).lease_file.exists()
    assert worker.exists()


def test_a_dead_lease_with_nothing_mid_round_is_simply_released(
    tmp_path, monkeypatch
):
    """A lane whose process died between rounds has no round to resume and no
    worker to judge. The lease goes — it is the only thing keeping the lane shut
    — and nothing else is looked at."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    worker, execution = seed_worker(config, "t-broken", real_repo=False)
    seed_lane(config, 1, phase=Phase.STOPPED, execution=execution)
    write_lease(config, 1, alive=False, boot=boot)
    before_worker = digest(worker)

    recovery = recover_dead_lanes(config, exclude=0)

    assert [e.action for e in recovery.lanes] == [RECOVERY_RELEASED]
    assert not lane_paths(config.state_dir, 1).lease_file.exists()
    assert digest(worker) == before_worker      # a terminal lane owns no round


def test_a_dead_lease_with_no_session_at_all_is_released(tmp_path, monkeypatch):
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    write_lease(config, 1, alive=False, boot=boot)

    recovery = recover_dead_lanes(config, exclude=0)

    assert [e.action for e in recovery.lanes] == [RECOVERY_RELEASED]
    assert not lane_paths(config.state_dir, 1).lease_file.exists()


def test_a_retired_lane_above_the_cap_is_recovered_too(tmp_path, monkeypatch):
    """`retired_lane_occupants`' own docstring hands this case here: lowering
    `[concurrency] lanes` does not end the session in a lane it cuts out, so a
    death there is one nothing else would ever see — and, before this, one that
    held the fleet's occupancy forever."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    _worker, execution = seed_worker(config, "t-cut", real_repo=True)
    seed_lane(config, 3, execution=execution)
    write_lease(config, 3, alive=False, boot=boot)

    recovery = recover_dead_lanes(config, exclude=0)

    assert [(e.lane_id, e.action) for e in recovery.lanes] == [
        (lane_id(3), RECOVERY_RESUMED)
    ]
    assert not lane_paths(config.state_dir, 3).lease_file.exists()


def test_an_unlistable_lanes_directory_refuses_but_still_frees_the_token(
    tmp_path, monkeypatch
):
    """Two fail-closed rules that must not be one. A `lanes/` directory that
    cannot be listed means no lane may be recovered — but the merge token is
    judged on ITS OWN record, so one unreadable directory cannot leave the whole
    fleet unable to merge."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / LANES_DIRNAME).write_text("a file where the lanes go\n")
    write_token(config, alive=False, boot=boot, lane=1)

    recovery = recover_dead_lanes(config, exclude=0)

    assert [e.action for e in recovery.lanes] == [RECOVERY_REFUSED]
    assert recovery.lanes[0].lane_index < 0      # not a lane; never mistaken for one
    assert recovery.merge_token == RECOVERY_RELEASED
    assert not merge_token_file(config.state_dir).exists()


def test_the_recovering_lane_is_never_offered_its_own_lane(tmp_path, monkeypatch):
    """A process holding a lane is alive in it by construction; a recovery that
    included itself would be recovering the round it is running."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    write_lease(config, 1, alive=False, boot=boot)

    assert [e.lane_index for e in dead_lane_survey(config, exclude=1)] == []
    assert [e.lane_index for e in dead_lane_survey(config, exclude=0)] == [1]


# =============================================================================
# 5. at one lane, none of it happens
# =============================================================================


def test_one_lane_recovers_nothing_and_reads_nothing(tmp_path, monkeypatch):
    """THE acceptance criterion. Even with a dead lease sitting in lane 0's own
    directory, a single-lane loop leaves it alone: nothing is surveyed, nothing
    is released, and nothing reaches the transcript."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=1)
    lease = write_lease(config, 0, alive=False, boot=boot)
    before = digest(lease.path)
    orch, _blockers = build_orchestrator(config, lane_index=0)

    orch._recover_dead_lanes()

    assert digest(lease.path) == before
    assert transcript_entries(config, "lane_recovered") == []
    assert transcript_entries(config, "merge_token_recovered") == []


def test_health_says_nothing_about_lanes_at_one_lane(tmp_path, monkeypatch):
    """`health`'s single-lane output is untouched: the field is `None`, which is
    what every reader that predates the fleet sees."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=1)
    write_lease(config, 0, alive=False, boot=boot)

    verdict = health_check(
        config,
        agent_probe=lambda *_a, **_k: False,
        work_probe=lambda *_a, **_k: False,
    )

    assert dead_lane_view(config) is None
    assert verdict.dead_lanes is None


def test_health_reports_a_dead_lane_without_turning_red(tmp_path, monkeypatch):
    """Above one lane the death is visible — a fleet one lane down and a fleet
    working must not read identically — and it is DATA rather than an alarm: the
    next tick recovers it, and a monitor that went red on every interrupted run
    is the alarm people learn to ignore."""
    boot = pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    _worker, execution = seed_worker(config, "t-dead", real_repo=True)
    seed_lane(config, 1, execution=execution)
    write_lease(config, 1, alive=False, boot=boot)

    view = dead_lane_view(config)

    assert view is not None
    assert [lane.lane_id for lane in view.lanes] == [lane_id(1)]
    assert view.lanes[0].task_id == "t-dead"
    assert view.lanes[0].phase == Phase.EXECUTING.value
    assert view.refused == ()
    assert lane_id(1) in view.describe()


def test_health_names_the_lane_nobody_can_read_as_one_needing_a_person(
    tmp_path, monkeypatch
):
    """The one case recovery refuses forever is the one `refused` exists to
    surface: nothing will clear it on its own, so it has to be nameable."""
    pin_boot(monkeypatch)
    config = make_config(tmp_path, lanes=2)
    write_lease(config, 1, alive=False, payload="{ torn")

    view = dead_lane_view(config)

    assert view is not None
    assert [lane.lane_id for lane in view.refused] == [lane_id(1)]
    assert "could not be read" in view.lanes[0].unreadable
