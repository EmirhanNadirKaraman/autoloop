"""Per-lane state files and lane leases — conc-05.

Candidate 4 of the nine in docs/AUTOLOOP.md, "Running several tasks at once —
the split plan". One claim, in three parts:

**N lanes hold N independent state machines; lane 0 at `lanes = 1` writes
literally `state.json` at today's path; two processes cannot enter one lane.**

1. **Lane 0 is not a new thing.** `state.lane_paths(state_dir, 0).state_file`
   is `AutoloopConfig.state_file`, and the two are pinned against EACH OTHER
   rather than either against a literal — a copy of `"state.json"` in a test
   agrees with both spellings on the day it is written and stops noticing the
   moment one of them moves. "No new file appears under the state dir" is
   asserted as a DIRECTORY LISTING, because that is what the sentence says; a
   path equality cannot see a lease file created beside it.
2. **Two lanes do not share a file.** Asserted over bytes: the other lane's
   state file must be byte-identical before and after, which catches a
   rewrite that happens to land on the same values as well as one that does
   not.
3. **A lane lease refuses everyone but the first.** Live refuses, a lease
   predating boot is dead HOWEVER ITS PID PROBES — so every such test uses
   `os.getpid()`, which is unquestionably alive, or the boot ordering is
   never exercised — and anything unreadable refuses rather than reading as a
   free lane.
4. **And the run that really runs a lane is the one holding it.** Section 6
   drives `cli._cmd_run` itself — the real fleet lock, the real startup sweep,
   the real `_run_locked`, only the phase machine doubled — and section 7
   drives `cli._run_continuous`, the loop `_cmd_run` hands the lane to, because
   a mechanism nothing acquires excludes nobody. The second entrant there is
   `cli._LaneEntry`, the same object the run entered with, and NOT a second
   `_cmd_run`: two of those can never reach a lease, since the fleet lock
   refuses the second first. Under the plan the two entrants of one lane are
   two lanes behind ONE supervisor holding ONE fleet lock, which is this shape.

No git repository, no subprocess and no agent: every claim here is about a path,
a small JSON file and a predicate. The one place a real process is needed is
"is this pid alive", and `os.getpid()` answers it for free.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autoloop import cli
from autoloop import lock as lock_module
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
from autoloop.errors import LockHeldError, StaleLockError, StateCorruptError
from autoloop.lock import (
    EXEC_HANDOFF_TOKEN_ENV,
    LOCK_FILENAME,
    LaneLease,
    LaneLeaseInfo,
    LockInfo,
    LoopLock,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import SELF_UPGRADE, Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    LANE_LEASE_SUFFIX,
    LANES_DIRNAME,
    STATE_FILENAME,
    LoopState,
    Phase,
    StateStore,
    lane_paths,
    lane_state_file,
    utcnow_iso,
)
from autoloop.tasks import TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger

URL = "https://chatgpt.com/c/lane-state-test"

#: The tree this process imports `autoloop` from, which is what a self-upgrade
#: record has to name to be APPLICABLE (`cli._package_root`). Spelled the same
#: way `test_self_upgrade.REPO_ROOT` spells it.
REPO_ROOT = Path(__file__).resolve().parents[2]


def make_config(tmp_path: Path, lanes: int = 1) -> AutoloopConfig:
    """The cheapest real config: `state_dir` is all these claims read, and
    `workers_root` is optional. Same shape as `test_stop_livelock.make_config`.

    `lanes` defaults to 1 — the deployment every existing caller here means, and
    the one where no lease is ever asked for."""
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        concurrency=ConcurrencyConfig(lanes=lanes),
    )


def a_state(session: str = "lane-test") -> LoopState:
    return LoopState(session_id=session, conversation_url=URL)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def lease_record(lane: str = "_lane-1", **overrides) -> dict:
    """A well-formed lease record, before whatever the caller breaks in it."""
    record = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": utcnow_iso(),
        "run_id": "run-under-test",
        "lane_id": lane,
        "state_dir": "/somewhere",
    }
    record.update(overrides)
    return record


def write_lease(lease: LaneLease, payload) -> None:
    """Put `payload` at the lease path — a dict is encoded, a string is written
    as-is (which is how the malformed cases get to be malformed)."""
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    lease.path.write_text(text, encoding="utf-8")


# ---- 1. lane 0 is today's state.json ----------------------------------------


def test_lane_zero_resolves_to_the_state_file_the_config_already_names(tmp_path):
    """The two spellings of one path, pinned against each other. `state.py`
    cannot import `config.py` (a real cycle — see `lane_paths`), so the name
    genuinely exists twice and this is what keeps the copies honest."""
    config = make_config(tmp_path)

    assert lane_state_file(config.state_dir, 0) == config.state_file
    assert lane_paths(config.state_dir, 0).state_dir == config.state_dir
    assert config.state_file.name == STATE_FILENAME


def test_one_lane_adds_no_file_to_the_state_dir(tmp_path):
    """THE acceptance criterion, as a directory listing rather than a path
    equality: after a lane-0 session has been saved and the fleet lock taken —
    everything a single-lane run touches here — the state dir holds the two
    files it has always held and no lease of any kind."""
    config = make_config(tmp_path)
    StateStore(lane_state_file(config.state_dir, 0)).save(a_state())

    with LoopLock(config.state_dir):
        listing = sorted(p.name for p in config.state_dir.iterdir())

    assert listing == [LOCK_FILENAME, STATE_FILENAME]
    assert not (config.state_dir / LANES_DIRNAME).exists()
    assert not lane_paths(config.state_dir, 0).lease_file.exists()
    assert not any(
        p.name.endswith(LANE_LEASE_SUFFIX) for p in config.state_dir.iterdir()
    )


def test_the_cli_loads_lane_zero_from_exactly_that_file(tmp_path):
    """`cli._load_state` resolves through the lane-aware resolver now. For lane
    0 that must be indistinguishable from what it did before — same path, same
    session — or every command in the CLI has quietly moved."""
    config = make_config(tmp_path)
    StateStore(config.state_file).save(a_state("cli-lane-zero"))

    store, state = cli._load_state(config)

    assert store.path == config.state_file
    assert state is not None and state.session_id == "cli-lane-zero"


def test_the_cli_can_be_pointed_at_another_lane(tmp_path):
    """The other half of the same function: a lane the session was not written
    to reads as an empty lane, not as lane 0's session."""
    config = make_config(tmp_path)
    StateStore(config.state_file).save(a_state("cli-lane-zero"))

    store, state = cli._load_state(config, 1)

    assert store.path == lane_state_file(config.state_dir, 1)
    assert store.path != config.state_file
    assert state is None


# ---- 2. N lanes, N independent state machines --------------------------------


def test_lane_one_lives_under_its_own_directory(tmp_path):
    paths = lane_paths(tmp_path, 1)

    assert paths.lane_id == lane_id(1)
    assert paths.state_dir == tmp_path / LANES_DIRNAME / lane_id(1)
    assert paths.state_file == paths.state_dir / STATE_FILENAME
    assert paths.lease_file == paths.state_dir / f"{lane_id(1)}{LANE_LEASE_SUFFIX}"


def test_each_lane_advances_without_touching_the_other_s_file(tmp_path):
    """Two lanes, two state machines. Asserted over BYTES rather than over the
    loaded phase: a writer that rewrote the neighbour's file with the same
    values would pass a phase comparison and has still written to a file it
    does not own."""
    zero = StateStore(lane_state_file(tmp_path, 0))
    one = StateStore(lane_state_file(tmp_path, 1))
    zero.save(a_state("zero"))
    one.save(a_state("one"))
    zero_bytes, one_bytes = zero.path.read_bytes(), one.path.read_bytes()
    assert zero.path != one.path

    advancing = zero.load()
    advancing.phase = Phase.AWAITING.value
    advancing.iteration = 7
    zero.save(advancing)
    zero_advanced = zero.path.read_bytes()

    assert zero_advanced != zero_bytes, "lane 0 really did advance"
    assert one.path.read_bytes() == one_bytes, "lane 1's file was rewritten"
    assert one.load().phase == Phase.READY.value
    assert one.load().session_id == "one"

    # ...and the same thing again from the other side, so neither lane is
    # merely the one that happened to move second.
    advancing = one.load()
    advancing.phase = Phase.EXECUTING.value
    one.save(advancing)

    assert zero.path.read_bytes() == zero_advanced, "lane 0's file was rewritten"
    assert zero.load().phase == Phase.AWAITING.value
    assert zero.load().iteration == 7
    assert one.load().phase == Phase.EXECUTING.value


@pytest.mark.parametrize("index", [False, True, 1.0, -1, "0", None])
def test_an_index_that_is_not_a_lane_is_refused(tmp_path, index):
    """`False == 0` in Python, so a validator that ran AFTER the lane-0 branch
    would hand a bool the one path in this package that must not move, and
    `1.0 == 1` would name lane 1's directory something else. The index is
    validated first, unconditionally."""
    with pytest.raises(ValueError):
        lane_paths(tmp_path, index)
    with pytest.raises(ValueError):
        lane_state_file(tmp_path, index)
    with pytest.raises(ValueError):
        LaneLease(tmp_path, index)


# ---- 3. two processes cannot enter one lane ----------------------------------


def test_a_live_lease_refuses_a_second_entrant(tmp_path):
    """The first entrant writes THIS pid on THIS host, so the refusal comes
    from the pid probe rather than from the foreign-host fail-closed branch —
    which would pass this test while proving nothing about a real second
    process on this machine."""
    held = LaneLease(tmp_path, 1).acquire()
    try:
        recorded = held.read()
        assert recorded is not None
        assert recorded.pid == os.getpid()
        assert recorded.hostname == socket.gethostname()
        assert LaneLease.is_live(recorded) is True

        with pytest.raises(LockHeldError) as excinfo:
            LaneLease(tmp_path, 1).acquire()
        assert lane_id(1) in str(excinfo.value)

        # ...and the incumbent's record is untouched by the refusal.
        assert held.read().run_id == held.run_id
    finally:
        held.release()

    assert not held.path.exists()
    assert LaneLease(tmp_path, 1).acquire().read().run_id != held.run_id


def test_a_second_lane_is_not_the_same_lane(tmp_path):
    """Exclusion is per lane, not per state dir — otherwise the fleet is one
    lane wearing N names."""
    first = LaneLease(tmp_path, 0).acquire()
    second = LaneLease(tmp_path, 1).acquire()

    assert first.path != second.path
    assert first.read().lane_id == lane_id(0)
    assert second.read().lane_id == lane_id(1)


def test_a_lease_predating_boot_is_dead_however_its_pid_probes(tmp_path, monkeypatch):
    """The reboot case, one lane down. The pid in the record is `os.getpid()` —
    alive beyond argument — so this only passes if `_predates_boot` is consulted
    BEFORE the probe. A dead pid here would pass for the wrong reason and the
    ordering would never be exercised."""
    boot = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: boot.timestamp())
    lease = LaneLease(tmp_path, 1)
    write_lease(
        lease,
        lease_record(started_at=iso(boot - timedelta(hours=2))),
    )

    recorded = lease.read()
    assert recorded.pid == os.getpid()
    assert lock_module._pid_alive(recorded.pid) is True, "the pid must be alive"
    assert LaneLease.is_live(recorded) is False

    # Dead, and STILL not taken over: a check-then-act steal is how two
    # processes both decide a dead lane is theirs. Recovery is explicit.
    with pytest.raises(StaleLockError) as excinfo:
        lease.acquire()
    message = str(excinfo.value)
    assert str(lease.path) in message
    assert "python -m autoloop unlock" not in message, (
        "that command breaks the FLEET lock and would not touch this file — "
        "a remedy that silently does nothing is worse than none"
    )
    assert lease.path.exists(), "a refusal must not remove the evidence"


def test_a_lease_from_another_host_is_live_and_refuses(tmp_path):
    """Fail-closed: a pid on a machine we cannot probe is not a pid we may
    declare dead. Same rule as the fleet lock's, because it IS the fleet
    lock's."""
    lease = LaneLease(tmp_path, 1)
    write_lease(lease, lease_record(hostname="some-other-host", pid=999_999))

    assert LaneLease.is_live(lease.read()) is True
    with pytest.raises(LockHeldError):
        lease.acquire()


def test_lane_liveness_is_the_fleet_lock_s_own_predicate(tmp_path, monkeypatch):
    """Pinned as a DELEGATION rather than by re-asserting the fleet lock's
    table: two implementations of "is it alive" agree on the day they are
    written, and the one that drifts is the one that lets two processes into
    one lane."""
    seen: list[object] = []

    def fake_is_live(info):
        seen.append(info)
        return True

    monkeypatch.setattr(lock_module.LoopLock, "is_live", staticmethod(fake_is_live))
    record = LaneLeaseInfo(
        pid=4242,
        hostname="a-host",
        started_at="2026-09-01T00:00:00+00:00",
        run_id="r",
        lane_id=lane_id(1),
        state_dir=str(tmp_path),
    )

    assert LaneLease.is_live(record) is True
    assert len(seen) == 1
    asked = seen[0]
    assert isinstance(asked, LockInfo)
    assert (asked.pid, asked.hostname, asked.started_at, asked.run_id) == (
        record.pid,
        record.hostname,
        record.started_at,
        record.run_id,
    )


#: Every way a lease record can be unreadable, and one entry per REASON rather
#: than per spelling. `pid: true` is here because `bool` is an `int` in Python
#: and would otherwise become pid 1; the unknown key is here because a
#: field-by-field reader would ignore it; the empty file is here because
#: `acquire` itself creates one for an instant.
CORRUPT_LEASES = {
    "empty file (a crash between O_EXCL and the write)": "",
    "truncated json": '{"pid": 1',
    "not an object": "[]",
    "a bare string": '"someone is in here, honest"',
    "missing run_id": {k: v for k, v in lease_record().items() if k != "run_id"},
    "an unknown key": {**lease_record(), "exec_handoff": {"pid": 1}},
    "pid as a string": lease_record(pid="4242"),
    "pid as a bool": lease_record(pid=True),
    "pid zero": lease_record(pid=0),
    "a negative pid": lease_record(pid=-1),
    "hostname as a number": lease_record(hostname=7),
    "started_at as null": lease_record(started_at=None),
    "another lane's lease": lease_record(lane="_lane-2"),
}


@pytest.mark.parametrize("case", sorted(CORRUPT_LEASES))
def test_a_corrupt_lease_refuses_rather_than_reading_as_free(tmp_path, case):
    """The fail-open this record shape exists to avoid. `LoopLock` answers a
    corrupt lock file with a not-live sentinel, which is safe there because
    removing it IS the documented recovery; here the same sentinel would make
    an unreadable lease indistinguishable from a well-formed dead one, and the
    recovery would then unlink a record that might name a live process. So all
    three verbs refuse — `read` alone would be a guard nobody consults — and
    the file is left exactly as it was found."""
    lease = LaneLease(tmp_path, 1)
    write_lease(lease, CORRUPT_LEASES[case])
    before = lease.path.read_bytes()

    with pytest.raises(StateCorruptError):
        lease.read()
    with pytest.raises(StateCorruptError):
        lease.acquire()
    with pytest.raises(StateCorruptError):
        lease.break_stale()

    assert lease.path.read_bytes() == before, "the file must be left untouched"


def test_an_absent_lease_is_a_free_lane_and_a_removed_one_is_too(tmp_path):
    """The other direction, so "refuses" is not just "always refuses": nothing
    on disk means the lane is free, which is the state every lane starts in."""
    lease = LaneLease(tmp_path, 1)

    assert lease.read() is None
    assert lease.acquire() is lease
    assert lease.path.exists()


@pytest.mark.parametrize("call", ["write", "fsync"])
def test_a_write_that_fails_takes_its_own_lease_back_off_disk(tmp_path, monkeypatch, call):
    """An ENOSPC between the `O_EXCL` and the record must not strand the lane.

    The file exists at that point but says nothing, so every later reader —
    `read`, `acquire`, `break_stale` — refuses it forever, which is a lane
    closed by an ordinary I/O error with no process in it. Removing it is the
    ONE unlink in this class that does not read the record first, and it is safe
    for the reason no other one is: `O_CREAT|O_EXCL` has just proved this call
    created the file, so there is no incumbent to steal from.

    The raise still propagates — a caller that could not write its lease is not
    in the lane — and the lane is enterable again immediately, which is the half
    that says nothing was left behind.
    """
    lease = LaneLease(tmp_path, 1)

    def boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lock_module.os, call, boom)

    with pytest.raises(OSError):
        lease.acquire()

    assert not lease.path.exists(), "an unreadable lease was left in the lane"
    monkeypatch.undo()
    assert LaneLease(tmp_path, 1).acquire().read().pid == os.getpid()


def test_a_short_write_still_lands_a_whole_lease(tmp_path, monkeypatch):
    """The failure that is not an exception. `os.write` is one `write(2)`: it
    may write FEWER bytes than it was given and RETURN that count, raising
    nothing. A single call treated as the whole record is therefore not an error
    path but the quiet one — a lease its owner believes it holds, truncated on
    disk, which every later `read` must refuse forever. That closes a lane with
    no process in it, and nothing says so.

    The fake writes seven bytes a call THROUGH THE REAL `os.write`, so what is
    asserted is the file that really lands rather than a description of it, and
    `len(chunks) > 1` is what keeps this honest: a test whose fake was never
    reached, or whose payload fitted in one chunk, would pass on any
    implementation at all — including the one-call version this exists to
    exclude.
    """
    real_write = os.write
    chunks: list[int] = []

    def short_write(fd, data):
        count = real_write(fd, data[:7])
        chunks.append(count)
        return count

    monkeypatch.setattr(lock_module.os, "write", short_write)
    lease = LaneLease(tmp_path, 1)

    acquired = lease.acquire()

    monkeypatch.undo()
    assert len(chunks) > 1, "the payload was written in one call — fake unused"
    assert sum(chunks) == lease.path.stat().st_size, "bytes went missing"
    record = lease.read()
    assert record is not None, "the record must be complete, not merely present"
    assert record.pid == os.getpid()
    assert record.run_id == acquired.run_id
    assert record.lane_id == lane_id(1)
    # ...and a record that is complete is a record that still excludes.
    with pytest.raises(LockHeldError):
        LaneLease(tmp_path, 1).acquire()


def test_a_write_that_makes_no_progress_refuses_rather_than_looping(
    tmp_path, monkeypatch
):
    """The other end of the same contract. A `write(2)` returning 0 has moved
    nothing, and it is neither an exception to propagate nor progress to build
    on: retrying it is a lane that hangs, and accepting it is a zero-byte lease
    that reads as acquired. So it is raised — as an `OSError`, which is the type
    the cleanup path catches. A raise of any other type would skip the unlink
    and strand exactly the empty record this is about, which is why the lease is
    asserted GONE rather than only the raise asserted.

    The fake gives up after a handful of calls so a looping implementation FAILS
    here instead of hanging a worker until something times out with no
    diagnosis.
    """
    lease = LaneLease(tmp_path, 1)
    calls: list[int] = []

    def no_progress(fd, data):
        calls.append(len(data))
        if len(calls) > 8:
            raise AssertionError(
                "acquire retried a zero-byte write instead of refusing"
            )
        return 0

    monkeypatch.setattr(lock_module.os, "write", no_progress)

    with pytest.raises(OSError) as excinfo:
        lease.acquire()

    monkeypatch.undo()
    assert calls, "the write was never attempted"
    assert str(lease.path) in str(excinfo.value)
    assert not lease.path.exists(), "an empty lease was left in the lane"
    # The lane is enterable again — nothing was left behind and nobody is in it.
    entrant = LaneLease(tmp_path, 1).acquire()
    assert entrant.read().pid == os.getpid()
    assert entrant.read().run_id == entrant.run_id


def test_release_removes_only_a_lease_this_process_still_owns(tmp_path):
    """`LoopLock.release`'s rule, for its reason: a lease somebody else has
    since recovered must not be deleted by the process that used to hold it."""
    lease = LaneLease(tmp_path, 1).acquire()
    write_lease(lease, lease_record(run_id="somebody-else"))

    lease.release()

    assert lease.path.exists()
    assert lease.read().run_id == "somebody-else"


def test_release_leaves_a_lease_it_can_no_longer_read(tmp_path):
    """Unreadable while we hold it: we can no longer prove the lease is ours,
    so it stays, and the lane refuses entry until a person looks. Failing the
    other way would clear a record that might name a live process."""
    lease = LaneLease(tmp_path, 1).acquire()
    lease.path.write_text("{not json", encoding="utf-8")

    lease.release()  # must not raise

    assert lease.path.exists()


def test_break_stale_refuses_a_live_lease_and_removes_a_dead_one(tmp_path, monkeypatch):
    """The recovery primitive the stale refusal describes — and the one the
    fleet supervisor's lane recovery (candidate 7) is meant to call, from
    inside the fleet lock. It refuses exactly what `acquire` refuses."""
    live = LaneLease(tmp_path, 1).acquire()
    with pytest.raises(LockHeldError):
        live.break_stale()
    assert live.path.exists()
    live.release()

    boot = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: boot.timestamp())
    dead = LaneLease(tmp_path, 1)
    write_lease(dead, lease_record(started_at=iso(boot - timedelta(hours=2))))

    removed = dead.break_stale()

    assert removed.lane_id == lane_id(1)
    assert not dead.path.exists()
    assert dead.acquire().read().pid == os.getpid()


def test_break_stale_on_an_empty_lane_says_so(tmp_path):
    with pytest.raises(StaleLockError):
        LaneLease(tmp_path, 1).break_stale()


# ---- 4. the fleet lock is untouched ------------------------------------------


def test_unlock_still_refuses_a_live_fleet_lock(tmp_path, monkeypatch):
    """The `unlock` command, unchanged: it breaks the FLEET lock, refuses a
    live one, and knows nothing about lanes."""
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    held = LoopLock(config.state_dir).acquire()
    try:
        with pytest.raises(LockHeldError):
            cli._cmd_unlock(argparse.Namespace(config=tmp_path / "config.toml"))
        assert held.path.exists()
    finally:
        held.release()


def test_the_fleet_lock_is_still_one_file_at_the_path_it_has_always_had(tmp_path):
    config = make_config(tmp_path)
    lock = LoopLock(config.state_dir)

    assert lock.path == config.state_dir / LOCK_FILENAME
    assert lock.path.name == "LOCK"


# ---- 5. an orchestrator knows which lane it is -------------------------------


def build_orchestrator(tmp_path: Path, **kwargs) -> Orchestrator:
    config = make_config(tmp_path)
    return Orchestrator(
        config=config,
        store=StateStore(config.state_file),
        state=a_state(),
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: None,
        registry=TaskRegistry([]),
        task_store=TaskStore(config.tasks_file),
        manifest_store=ManifestStore(config.manifests_dir),
        **kwargs,
    )


def test_an_orchestrator_is_lane_zero_unless_told_otherwise(tmp_path):
    assert build_orchestrator(tmp_path).lane_index == 0
    assert build_orchestrator(tmp_path).lane_id == lane_id(0)
    assert build_orchestrator(tmp_path, lane_index=2).lane_id == lane_id(2)


@pytest.mark.parametrize("index", [False, True, -1, 1.0, "1"])
def test_an_orchestrator_refuses_an_index_that_is_not_a_lane(tmp_path, index):
    with pytest.raises(ValueError):
        build_orchestrator(tmp_path, lane_index=index)


# ---- 6. the run that runs a lane is the one holding it -----------------------


def run_args() -> argparse.Namespace:
    """What `_cmd_run` -> `_run_locked` reads: one ordinary round, no operator
    flags. `--continuous` is off, because the single-round path is the shorter
    of the two through the same `_LaneEntry` in `_cmd_run`."""
    return argparse.Namespace(
        config=None,
        continuous=False,
        max_steps=None,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
    )


class LaneWork:
    """Stands in for the phase machine, and for nothing else.

    `cli._cmd_run` and `cli._run_locked` are the REAL ones in every test below:
    the config is loaded, the fleet lock taken and released, the startup sweep
    run, the state and registry loaded, the summary printed, the heartbeat
    published. Only the orchestrator is doubled — and its `run()` is the one
    instant "while this lane is working" exists in, which is where a second
    entrant has to be refused.
    """

    def __init__(self, state: LoopState, during):
        self.state = state
        self.steps_taken = 0
        self._during = during

    def run(self, max_steps=None) -> str:
        self._during()
        return Phase.STOPPED.value

    def decline_self_upgrade(self, sha) -> bool:  # pragma: no cover - no boundary
        return False


def drive_a_run(root: Path, monkeypatch, lanes: int, during) -> AutoloopConfig:
    """One real `run` over a real state dir, calling `during(config)` while the
    lane's work is in flight. Returns the config it ran against."""
    config = make_config(root, lanes=lanes)
    StateStore(config.state_file).save(a_state("lane-wiring"))
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda cfg, args, store, state, task_store, registry: LaneWork(
            state, lambda: during(cfg)
        ),
    )

    assert cli._cmd_run(run_args()) == 0

    return config


def test_a_run_at_two_lanes_refuses_a_second_entrant_to_its_lane(tmp_path, monkeypatch):
    """THE third part of the claim, through the code that really runs a lane.

    The second entrant is `cli._LaneEntry` — the same object `_cmd_run` entered
    the lane with — and deliberately not a second `_cmd_run`: two of those can
    never reach a lease at all, because the fleet lock refuses the second one
    first. Under docs/AUTOLOOP.md's "Decision 2" the two entrants of one lane
    are two lanes behind ONE supervisor holding ONE fleet lock, and this is that
    shape with the supervisor's second entry made by hand.

    The refusal is checked to name the LEASE. A test that accepted any
    `LockHeldError` here would pass just as happily on the fleet lock's, and
    would be proving an exclusion that has existed since before lanes did.
    """
    seen: dict = {}

    def second_attempt(config):
        lease_path = lane_paths(config.state_dir, 0).lease_file
        seen["held"] = json.loads(lease_path.read_text(encoding="utf-8"))
        with pytest.raises(LockHeldError) as excinfo:
            with cli._LaneEntry(config):
                seen["entered"] = True
        seen["refusal"] = str(excinfo.value)
        seen["survivor"] = json.loads(lease_path.read_text(encoding="utf-8"))

    config = drive_a_run(tmp_path, monkeypatch, 2, second_attempt)
    lease_path = lane_paths(config.state_dir, 0).lease_file

    assert "entered" not in seen, "a second process got into an occupied lane"
    assert seen["held"]["pid"] == os.getpid()
    assert seen["held"]["lane_id"] == lane_id(0)
    assert lane_id(0) in seen["refusal"]
    assert str(lease_path) in seen["refusal"], "the refusal must name the lease"
    assert str(config.state_dir / LOCK_FILENAME) not in seen["refusal"], (
        "a refusal naming the fleet lock would mean this passed on the wrong "
        "exclusion — the fleet lock refuses a second PROCESS, never a second "
        "entry to one lane behind one holder of it"
    )
    assert seen["survivor"]["run_id"] == seen["held"]["run_id"], (
        "and the incumbent's own lease is untouched by the refusal"
    )
    assert not lease_path.exists(), "the lane is released when the run ends"


def test_one_lane_adds_nothing_to_the_state_dir_that_two_lanes_do_not(
    tmp_path, monkeypatch
):
    """THE acceptance criterion, through the production path, as a DIFFERENCE.

    Two identical runs over two identical state dirs, one at `lanes = 1` and one
    at `lanes = 2`, listed at the same instant — while the lane's work is in
    flight, which is the only moment a lease exists at all. Everything a run
    writes cancels out, so what is left is exactly what having lanes adds: one
    lease file, and at one lane not even that.
    """
    listings: dict[int, list[str]] = {}

    def listing_at(lanes):
        def during(config):
            listings[lanes] = sorted(p.name for p in config.state_dir.iterdir())
        return during

    one = drive_a_run(tmp_path / "one", monkeypatch, 1, listing_at(1))
    two = drive_a_run(tmp_path / "two", monkeypatch, 2, listing_at(2))

    assert set(listings[2]) - set(listings[1]) == {f"{lane_id(0)}{LANE_LEASE_SUFFIX}"}
    assert set(listings[1]) - set(listings[2]) == set()
    assert not any(name.endswith(LANE_LEASE_SUFFIX) for name in listings[1])
    assert STATE_FILENAME in listings[1] and LOCK_FILENAME in listings[1], (
        "the single-lane run really did take the fleet lock and write literally "
        "state.json — otherwise the difference above is a difference between "
        "two runs that did nothing"
    )
    for config in (one, two):
        assert not any(
            p.name.endswith(LANE_LEASE_SUFFIX) for p in config.state_dir.iterdir()
        ), "nothing is left behind after either run"
        assert not (config.state_dir / LANES_DIRNAME).exists()


def test_at_one_lane_the_entry_holds_nothing_and_still_answers_every_verb(tmp_path):
    """The gate itself. At one lane there is no lease to release for a handoff
    and none to take back, so both verbs are no-ops that answer as if the lane
    were held — a caller must not have to know which deployment it is in."""
    entry = cli._LaneEntry(make_config(tmp_path, lanes=1))

    with entry as entered:
        assert entered is entry
        assert entry.enabled is False
        assert entry.lease is None
        assert entry.release_for_handoff() is True
        entry.reenter_after_handoff()
        assert entry.lease is None

    assert not (tmp_path / ".al").exists(), "not so much as a directory was made"


# ---- 7. the lane and the self-upgrade handoff --------------------------------


def upgrade_config(root: Path, lanes: int = 2) -> AutoloopConfig:
    """A config with a pending upgrade naming the tree this process really
    imports from — which is what makes the boundary APPLICABLE rather than
    answered `unapplicable` before it reaches the handoff."""
    config = make_config(root, lanes=lanes)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    UpgradeStore(config.pending_upgrade_file).save(
        PendingUpgrade(
            base_sha="b" * 40,
            previous_base_sha="a" * 40,
            candidate_sha="c" * 40,
            task_id="conc-05",
            repo_root=str(REPO_ROOT),
            paths=["autoloop/lock.py"],
            status=UPGRADE_PENDING,
            recorded_at=utcnow_iso(),
        )
    )
    return config


def upgrade_details(config) -> list[str]:
    """The `detail` of every `self_upgrade_exec_failed` entry in the
    transcript — a boundary that ends in silence is the one shape this path
    exists to prevent, so the outcome is read from the log rather than only from
    the return value."""
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [
        r["data"].get("detail", "")
        for r in rows
        if r["type"] == f"self_upgrade_{UPGRADE_EXEC_FAILED}"
    ]


class StopTheLoop(Exception):
    """Ends `_run_continuous` from inside iteration two — reaching it at all is
    half the assertion, since a boundary that killed the loop would not."""


class BoundaryThenStop:
    """Offers the self-upgrade boundary once, then ends the run.

    The round counter is SHARED rather than an attribute: `_run_continuous`
    rebuilds the orchestrator every iteration, so a per-object counter would
    reset and offer the boundary forever.
    """

    def __init__(self, state: LoopState, rounds: list):
        self.state = state
        self.steps_taken = 0
        self._rounds = rounds

    def run(self, *_args, **_kwargs) -> str:
        self._rounds.append(len(self._rounds))
        if len(self._rounds) >= 2:
            raise StopTheLoop()
        return SELF_UPGRADE

    def decline_self_upgrade(self, sha) -> bool:
        return True


def test_the_lane_is_free_at_the_instant_of_the_exec(tmp_path, monkeypatch):
    """`os.execv` does not unwind, so a lease held across it survives into the
    successor as a LIVE lease naming the successor's own pid — and the successor
    fails closed on its own lane, which ends the run. The lease therefore goes
    before the exec, and this observes the file from inside the replacement
    itself.

    Driven through `cli._run_continuous`, the loop that really reaches the
    boundary, so what is pinned is the WIRING and not only the callee: a lane
    that never travelled from `_cmd_run` to the decision would leave the lease
    on disk at the moment `os.execv` was called, which is the assertion below.

    The second half is the exec that does NOT happen: the process carries on
    doing lane work with the code it started with, so it has to be back in its
    lane — under a new run id, because it is a new acquisition and not a
    resurrection of the old record.
    """
    config = upgrade_config(tmp_path)
    StateStore(config.state_file).save(a_state("upgrade-lane"))
    lock = LoopLock(config.state_dir).acquire()
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    rounds: list = []
    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda cfg, args, store, state, task_store, registry: BoundaryThenStop(
            state, rounds
        ),
    )
    lease_path = lane_paths(config.state_dir, 0).lease_file
    observed: dict = {}

    def refuse_to_exec(path, argv):
        observed["lease_at_exec"] = lease_path.exists()
        raise OSError("no successor today")

    monkeypatch.setattr(os, "execv", refuse_to_exec)
    args = argparse.Namespace(config=None, continuous=True, null_executor=False)
    try:
        with cli._LaneEntry(config) as lane:
            held = json.loads(lease_path.read_text(encoding="utf-8"))

            with pytest.raises(StopTheLoop):
                cli._run_continuous(args, config, lock, lane)

            assert observed["lease_at_exec"] is False, (
                "the successor would have found a live lease naming its own pid"
            )
            assert lease_path.exists(), "an exec that did not happen re-enters the lane"
            back = json.loads(lease_path.read_text(encoding="utf-8"))
            assert back["pid"] == os.getpid()
            assert back["run_id"] != held["run_id"]
            assert lane.lease is not None and lane.lease.run_id == back["run_id"]
    finally:
        lock.release()

    assert not lease_path.exists(), "and the lane is released with the run"
    assert EXEC_HANDOFF_TOKEN_ENV not in os.environ
    assert rounds == [0, 1], "the loop carried on past the boundary"
    assert upgrade_details(config), "the boundary is never left unlogged"


def test_a_lane_that_cannot_be_released_refuses_the_handoff(tmp_path, monkeypatch):
    """Fail-closed, and the exact symmetry of the lock that cannot be armed: a
    lease still on disk would be one the successor must refuse, so there is no
    point replacing the process — and every reason not to. The lock is disarmed
    again with it, or the token would outlive the handoff it authorised.

    The lease is made unreleasable the way it really becomes unreleasable: it
    stops being ours, so `LaneLease.release` leaves it exactly as it is."""
    config = upgrade_config(tmp_path)
    lock = LoopLock(config.state_dir).acquire()
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))

    def must_not_exec(*_args, **_kwargs):
        raise AssertionError("the replacement must not be attempted")

    monkeypatch.setattr(os, "execv", must_not_exec)
    try:
        with cli._LaneEntry(config) as lane:
            write_lease(
                lane.lease, lease_record(lane=lane_id(0), run_id="somebody-else")
            )

            outcome = cli._self_upgrade_at_boundary(config, lock, None, lane)

            assert outcome == UPGRADE_EXEC_FAILED
            assert any("lane lease" in detail for detail in upgrade_details(config))
            assert lane.lease is not None, "the lane was not given up"
            assert EXEC_HANDOFF_TOKEN_ENV not in os.environ
            assert lock.read().exec_handoff is None, "the lock is disarmed again"
            record = UpgradeStore(config.pending_upgrade_file).load()
            assert record.status == UPGRADE_PENDING, "still retryable by the next process"
    finally:
        lock.release()
