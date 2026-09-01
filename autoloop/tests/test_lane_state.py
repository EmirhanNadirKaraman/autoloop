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
from autoloop.config import AutoloopConfig, BrowserConfig, lane_id
from autoloop.errors import LockHeldError, StaleLockError, StateCorruptError
from autoloop.lock import LOCK_FILENAME, LaneLease, LaneLeaseInfo, LockInfo, LoopLock
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
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


def make_config(tmp_path: Path) -> AutoloopConfig:
    """The cheapest real config: `state_dir` is all these claims read, and
    `workers_root` is optional. Same shape as `test_stop_livelock.make_config`."""
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
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
