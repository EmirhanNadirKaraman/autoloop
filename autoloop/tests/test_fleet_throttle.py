"""One account allowance, one fleet-wide throttle — conc-11.

Candidate 10 of docs/AUTOLOOP.md's split plan, and the one the plan itself is
silent about: `conc-05` gives every lane its own state file, so
`LoopState.rate_limit_backoffs` and `rate_limit_retry_not_before` become N
independent counters against ONE ChatGPT account allowance. At four lanes that
is four copies of the same deterministic 60s, four re-probes on the same tick,
and up to 4 x 6 = 24 throttled attempts spent discovering one limit a single
lane would have found in 6 — every one of them a request against an allowance
already known to be exhausted.

**THE CLAIM: N lanes throttled by one limit produce ONE backoff episode, not
N.**

Six parts, and each section below is one of them:

1. **Coalescing.** Two lanes throttled inside one window leave ONE episode and
   ONE increment behind them, not two — asserted on the fleet record, on both
   lanes' own counters, and on the delay the second lane actually waits (the
   REMAINDER of the open window, not a fresh copy of it).
2. **Admission.** `FleetSupervisor.plan` holds every READY task while the
   window is open and admits again once it expires, and holds — never
   passes — when the record cannot be read at all.
3. **Spread.** N lanes do not re-probe on the same instant. The persisted
   stamps are exact here (`stamp = now + (deadline + jitter - now)`), so this
   is an equality rather than a tolerance.
4. **One budget for the fleet.** `max_rate_limit_backoffs` is checked against
   the SHARED episode number, by a lane that merely JOINED an episode as much
   as by the one that opened it — otherwise the opener parks while the other
   three wait forever on a fleet that has already stopped.
5. **Durability, and the poison pill it must not become.** The record outlives
   the process; a throttle arriving long after the last window closed starts a
   fresh streak rather than inheriting a parked one.
6. **And at `lanes = 1` none of it exists.** No file is written, no file is
   read, the delay sequence and the persisted stamp are today's, and the park
   text is byte-identical.

Three sections sit outside that six: the setting and what `health` shows; the
one shape of requirement 2 that does not arrive through the queue at all — an
EMPTY queue, where the caller's hold condition has nothing to hold, so the term
that answers it is the plan's own `fleet_throttled` rather than its `held`; and
ENDING an episode, the half of the record's lifecycle the six do not reach.
Opening and joining are decided under the mutex; the clear was not, and an
unconditional one lets a lane that waited out episode *n* delete episode *n + 1*
between another lane's opening it and its own completed step — a live deadline
and an escalated counter erased by a lane that never saw either.

No git repository, no subprocess and no agent: every claim here is about a
small JSON file, a registry and a handler. The two places more than that is
needed are section 1's last test, which drives two THREADS through `observe` at
once — the read-modify-write there is the whole of the coalescing, and a
single-threaded test cannot see it lose the race — and the last section, which
drives the real `cli._run_continuous`, because "no session is opened" is a claim
about the loop and not about the supervisor it asks.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autoloop.config import (
    MAX_RELEASE_JITTER_SECONDS,
    AutoloopConfig,
    BrowserConfig,
    ConcurrencyConfig,
    ConversationConfig,
    load_config,
)
from autoloop.errors import ConfigError, RateLimitedError, StateCorruptError
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    FLEET_THROTTLE_UNREADABLE,
    HOLD_AT_CAP,
    HOLD_DRAINING,
    HOLD_RATE_LIMITED,
    FleetSupervisor,
    Orchestrator,
    release_jitter_seconds,
    session_task_id,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    FleetThrottle,
    FleetThrottleStore,
    LoopState,
    Phase,
    StateStore,
    fleet_throttle_file,
    lane_state_file,
)
from autoloop.tasks import CO_SCHEDULE_EXEMPT_PATHS, Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop import cli
from autoloop import health as health_module
from autoloop import orchestrator as orchestrator_module

URL = "https://chatgpt.com/c/fleet-throttle-test"

#: The default first back-off, spelled once. Every timing assertion below is
#: derived from this rather than from a literal 60, so a changed default moves
#: the expectations with it instead of leaving them silently wrong.
BASE = 60.0
JITTER = 5.0


def make_config(tmp_path: Path, lanes: int = 2, **overrides) -> AutoloopConfig:
    """The cheapest real config: a state dir, a fleet size and the back-off
    schedule under test."""
    policy = overrides.pop("policy", PolicyConfig())
    jitter = overrides.pop("rate_limit_release_jitter_seconds", JITTER)
    return AutoloopConfig(
        browser=BrowserConfig(
            conversation_url=URL,
            rate_limit_backoff_seconds=overrides.pop("base", BASE),
            rate_limit_backoff_max_seconds=overrides.pop("ceiling", 600.0),
        ),
        policy=policy,
        state_dir=tmp_path / ".al",
        concurrency=ConcurrencyConfig(
            lanes=lanes, rate_limit_release_jitter_seconds=jitter
        ),
        # Not browser-backed, so `_handle_rate_limited` takes the "this run
        # drives no browser" branch and needs no client, no page and no probe.
        # Nothing here is a claim about the browser.
        conversation=ConversationConfig(provider="codex_cli"),
    )


def build_lane(config: AutoloopConfig, lane_index: int = 0, tasks=()):
    """One lane's orchestrator over the shared state dir, with its sleeps
    recorded rather than taken.

    Its state file is that LANE's (`lane_state_file`), which is the whole point:
    conc-05 gave every lane its own, and the two rate-limit fields inside it are
    the N independent counters this task replaces with one shared record.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(lane_state_file(config.state_dir, lane_index))
    state = LoopState(session_id=f"lane-{lane_index}", conversation_url=URL)
    state.phase = Phase.AWAITING.value
    store.save(state)
    registry = TaskRegistry(list(tasks))
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
        client_factory=lambda: None,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        lane_index=lane_index,
    )
    taken: list[float] = []
    orch._sleep = taken.append
    return orch, taken


def throttle_store(config: AutoloopConfig) -> FleetThrottleStore:
    return FleetThrottleStore(fleet_throttle_file(config.state_dir))


def a_task(task_id: str, paths=CO_SCHEDULE_EXEMPT_PATHS) -> Task:
    return Task(
        id=task_id,
        title=f"task {task_id}",
        description="a task the supervisor may or may not admit",
        approved_paths=tuple(paths),
    )


def registry_of(*tasks: Task) -> TaskRegistry:
    registry = TaskRegistry()
    registry.add_many(list(tasks))
    return registry


def throttled(orch: Orchestrator) -> None:
    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))


def throttled_stamp(orch: Orchestrator) -> datetime:
    """Throttle `orch` and answer with the deadline it PERSISTED, read from
    inside the wait.

    It has to be read there and cannot be read after: `_serve_rate_limit_wait`
    clears `rate_limit_retry_not_before` the moment the wait is served, which is
    the same reason the durability tests in `test_rounds_and_restart.py` inspect
    the store from a sleep callback."""
    seen: list[str] = []
    previous = orch._sleep
    orch._sleep = lambda _seconds: seen.append(
        orch._store.load().rate_limit_retry_not_before
    )
    try:
        throttled(orch)
    finally:
        orch._sleep = previous
    assert len(seen) == 1 and seen[0], "the lane recorded no deadline"
    return datetime.fromisoformat(seen[0])


def entries(config: AutoloopConfig, event: str) -> list[dict]:
    """The `data` payloads of every transcript entry of one type."""
    path = config.transcript_file
    if not path.exists():
        return []
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("type") == event:
            found.append(record.get("data") or {})
    return found


def park_code(config: AutoloopConfig):
    """The code a park recorded. It lives on the transcript's `needs_user`
    entry — never on `LoopState`, which carries only the kind."""
    rows = entries(config, "needs_user")
    return rows[-1]["code"] if rows else None


# ---- 1. two lanes, one limit, one episode -------------------------------------


def test_two_lanes_throttled_in_one_window_are_one_episode(tmp_path):
    """THE claim. Both lanes meet the same account limit inside one window: the
    fleet's counter moves ONCE, and each lane's own counter reads that shared
    number rather than its private tally of one."""
    config = make_config(tmp_path, lanes=2)
    lane0, taken0 = build_lane(config, 0)
    lane1, taken1 = build_lane(config, 1)

    throttled(lane0)
    throttled(lane1)

    record = throttle_store(config).load()
    assert record.backoffs == 1, "one limit, one episode"
    assert record.observations == 2, "met by two lanes, counted once"
    assert record.opened_by == lane0.lane_id
    assert lane0.state.rate_limit_backoffs == 1
    assert lane1.state.rate_limit_backoffs == 1, (
        "the joining lane reports the FLEET's episode, not a second one"
    )
    # The opener owes the whole window; the joiner owes what is LEFT of it plus
    # its own release offset — never a fresh copy of the wait.
    assert len(taken0) == 1 and taken0[0] == pytest.approx(BASE, abs=0.5)
    assert len(taken1) == 1
    assert taken1[0] == pytest.approx(
        BASE + release_jitter_seconds(1, 2, JITTER), abs=2.0
    )


def test_the_second_lane_does_not_extend_the_shared_deadline(tmp_path):
    """A joiner that pushed the deadline out would turn N lanes into a window N
    times as long — the same miscount as N episodes, arriving as one long one."""
    config = make_config(tmp_path, lanes=3)
    lane0, _ = build_lane(config, 0)
    lane1, _ = build_lane(config, 1)
    lane2, _ = build_lane(config, 2)

    throttled(lane0)
    opened = throttle_store(config).load().retry_not_before
    throttled(lane1)
    throttled(lane2)

    record = throttle_store(config).load()
    assert record.retry_not_before == opened, "the window is the one that opened"
    assert (record.backoffs, record.observations) == (1, 3)


def test_a_joiner_never_waits_longer_than_the_schedule_prescribes(tmp_path):
    """The remainder a joiner serves is CLAMPED, and the clamp is not cosmetic.

    The shared deadline comes off a file N processes write and an operator can
    hand-edit, and the sleep happens INSIDE a step — between two heartbeats. A
    stamp a year out (a bad edit, or a backward system-clock jump, which look
    identical from here) would otherwise become a year-long sleep in a loop
    whose monitor calls 45 minutes stale, which is the same reason
    `_await_rate_limit_deadline` clamps its own remainder.

    Bounded rather than refused, deliberately: the lane re-probes on the
    schedule and JOINS the same episode again, so a nonsense deadline costs
    extra observations and never an extra episode.
    """
    config = make_config(tmp_path, lanes=2)
    lane1, taken = build_lane(config, 1)
    seed_episode(config, backoffs=1, opens_in=365 * 24 * 3600)

    throttled(lane1)

    offset = release_jitter_seconds(1, 2, JITTER)
    assert taken == [BASE + offset], "one schedule, not one year"
    record = throttle_store(config).load()
    assert (record.backoffs, record.observations) == (1, 2)


def test_a_window_that_has_closed_escalates_exactly_as_one_lane_would(tmp_path):
    """Coalescing must not become 'never escalate'. A throttle after the window
    has closed opens the NEXT episode, and the sequence the fleet sees is the
    single-lane sequence — 60, 120, 240 — not a fixed interval."""
    config = make_config(tmp_path, lanes=4)
    store = throttle_store(config)
    lane0, _ = build_lane(config, 0)
    # Whole seconds, so the millisecond truncation `retry_not_before` applies is
    # a no-op and the schedule can be asserted as an equality.
    now = datetime.now(timezone.utc).replace(microsecond=0)

    sequence = []
    for _ in range(3):
        record = store.observe(delay_for=lane0._rate_limit_delay, lane_id="_lane-0", now=now)
        sequence.append((record.backoffs, record.deadline() - now))
        # Just past the window it opened, which is where the next re-probe lands.
        now = record.deadline() + timedelta(seconds=1)

    assert [backoffs for backoffs, _ in sequence] == [1, 2, 3]
    assert [delay.total_seconds() for _, delay in sequence] == [60.0, 120.0, 240.0]


def test_two_threads_opening_at_once_still_open_one_episode(tmp_path):
    """The race the whole record exists to lose. Two lanes reaching `observe` at
    the same instant is exactly the read-modify-write that, done outside a mutex,
    reads the same absent record twice and writes episode 1 twice — the counting
    bug rebuilt inside its own fix.

    Threads rather than processes because the mutex is both: an in-process
    `RLock` and an `flock`, and the in-process half is the one a thread test can
    actually contend."""
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    store = throttle_store(config)
    ready = threading.Barrier(2)
    seen: list = []

    def observe(lane: str):
        ready.wait(timeout=5)
        seen.append(store.observe(delay_for=lane0._rate_limit_delay, lane_id=lane))

    threads = [threading.Thread(target=observe, args=(f"_lane-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(seen) == 2, "both observations completed"
    record = store.load()
    assert record.backoffs == 1, "one episode, whichever thread got there first"
    assert record.observations == 2
    assert {r.backoffs for r in seen} == {1}


# ---- 2. admission ------------------------------------------------------------


def test_no_lane_is_admitted_while_the_fleet_window_is_open(tmp_path):
    """Requirement 2: the supervisor must not admit a lane into a window another
    lane opened. Every READY task is held, with a reason a reader can match on,
    and every one of them stays `pending` and unattempted."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"), a_task("t2"))
    TaskStore(config.tasks_file).save(registry)
    lane0, _ = build_lane(config, 0)
    throttled(lane0)

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert plan.admitted == ()
    assert plan.fleet_throttled
    assert all(HOLD_RATE_LIMITED in reason for _, reason in plan.held)
    assert [task.status for task in registry.all_tasks()] == ["pending", "pending"]
    assert not config.executions_dir.exists(), "no attempt was charged"
    assert HOLD_RATE_LIMITED in plan.describe()


def test_the_same_fleet_admits_again_once_the_window_expires(tmp_path):
    """The other half of the same claim: the hold LIFTS. Asked at an instant
    past the shared deadline, with nothing else changed."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"), a_task("t2"))
    TaskStore(config.tasks_file).save(registry)
    lane0, _ = build_lane(config, 0)
    throttled(lane0)
    deadline = throttle_store(config).load().deadline()

    supervisor = FleetSupervisor.from_config(config)
    inside = supervisor.plan(registry, now=deadline - timedelta(seconds=1))
    after = supervisor.plan(registry, now=deadline + timedelta(seconds=1))

    assert inside.admitted == () and inside.fleet_throttled
    assert [task.id for task in after.admitted] == ["t1", "t2"]
    assert after.held == () and not after.fleet_throttled


def test_planning_reads_the_record_and_writes_nothing(tmp_path):
    """A plan is a VALUE. The cap's own test asserts the state directory is
    byte-identical around `plan()`, and a throttle reader that CREATED its file
    would break that for every fleet that has never been throttled."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"))
    TaskStore(config.tasks_file).save(registry)
    before = {
        str(p.relative_to(config.state_dir)): p.read_bytes()
        for p in sorted(config.state_dir.rglob("*"))
        if p.is_file()
    }

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert [task.id for task in plan.admitted] == ["t1"]
    assert not fleet_throttle_file(config.state_dir).exists()
    assert {
        str(p.relative_to(config.state_dir)): p.read_bytes()
        for p in sorted(config.state_dir.rglob("*"))
        if p.is_file()
    } == before


def test_a_record_nobody_can_read_holds_rather_than_admits(tmp_path):
    """The fail-open this gate is most likely to ship: a corrupt record read as
    'no throttle in progress' would admit the whole fleet into a limit nobody
    could see. Nothing known is not permission."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"))
    TaskStore(config.tasks_file).save(registry)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    fleet_throttle_file(config.state_dir).write_text("{not json", encoding="utf-8")

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert plan.admitted == ()
    assert plan.throttled_until == FLEET_THROTTLE_UNREADABLE
    assert plan.hold_reason("t1").startswith(HOLD_RATE_LIMITED)
    assert "cannot be read" in plan.hold_reason("t1")


def test_the_supervisor_never_raises_on_a_record_it_cannot_read(tmp_path):
    """`cli._fleet_plan` has no `try` around this call, so a raise here would
    leave `_run_continuous` by traceback — no park, no blocker, no heartbeat,
    which is the one exit shape this loop went out of its way to eliminate. The
    directory-where-a-file-should-be case is the one that raises `OSError`
    rather than a decode error, so it is the one asserted."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"))
    fleet_throttle_file(config.state_dir).mkdir(parents=True)

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert plan.throttled_until == FLEET_THROTTLE_UNREADABLE
    assert plan.admitted == ()


def test_a_drain_outranks_a_throttle_in_what_is_reported(tmp_path):
    """Both hold everything, so the order only decides which WORD a reader gets.
    A drain ends — the fleet empties and the boundary is taken — while a throttle
    merely expires, so a fleet holding for both reports the one it is going
    somewhere with."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"))
    lane0, _ = build_lane(config, 0)
    throttled(lane0)

    plan = FleetSupervisor.from_config(config).plan(registry, upgrade_pending=True)

    assert plan.hold_reason("t1") == HOLD_DRAINING
    assert plan.fleet_throttled, "and the throttle is still reported in the record"


def test_a_lane_already_mid_round_is_not_stopped_by_the_window(tmp_path):
    """The task's own boundary: a lane already mid-round when the window opens
    finishes or parks by the existing rules, and it is ADMISSION that stops. So
    the busy lane keeps its phase and its round, and only the queue is held."""
    config = make_config(tmp_path, lanes=2)
    registry = registry_of(a_task("t1"))
    lane0, _ = build_lane(config, 0)
    lane1, _ = build_lane(config, 1)
    lane1.state.phase = Phase.EXECUTING.value
    lane1._store.save(lane1.state)
    throttled(lane0)

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert plan.admitted == () and plan.hold_reason("t1").startswith(HOLD_RATE_LIMITED)
    assert Phase(lane1._store.load().phase) is Phase.EXECUTING, "untouched"


# ---- 3. the release is spread -------------------------------------------------


def test_release_times_for_n_lanes_are_spread_not_identical(tmp_path):
    """Without this, one shared window is one shared re-probe: three lanes wake
    on the same tick and hit an allowance that has just run out, together.

    The stamps are EXACT, not approximate: each lane persists `now + (deadline +
    jitter - now)`, which is the shared deadline plus its own offset however
    long the wall clock took to get there."""
    config = make_config(tmp_path, lanes=3)
    lanes = [build_lane(config, index)[0] for index in range(3)]

    stamps = [throttled_stamp(lane) for lane in lanes]

    deadline = throttle_store(config).load().deadline()
    assert len(set(stamps)) == 3, "three lanes, three release instants"
    for index, stamp in enumerate(stamps):
        offset = (stamp - deadline).total_seconds()
        assert offset == pytest.approx(
            release_jitter_seconds(index, 3, JITTER), abs=0.01
        )
        assert 0 <= offset < JITTER, "bounded, so `health` can state it"
    assert abs((stamps[0] - deadline).total_seconds()) < 0.01, (
        "lane 0 releases on the shared deadline itself"
    )


def test_the_spread_is_zero_at_one_lane_and_for_lane_zero():
    """The `lanes = 1` criterion made structural, plus the property that keeps
    the fleet's own first lane on the deadline an operator reads."""
    assert release_jitter_seconds(0, 1, JITTER) == 0.0
    assert release_jitter_seconds(3, 1, JITTER) == 0.0
    assert release_jitter_seconds(0, 4, JITTER) == 0.0
    assert release_jitter_seconds(1, 4, 0.0) == 0.0, "a zero ceiling is no spread"


def test_the_spread_is_bounded_by_the_configured_ceiling():
    """The promise the setting's own documentation makes: strictly less than the
    value, for every lane of every fleet size this build can run."""
    for lanes in range(1, 9):
        for index in range(lanes + 3):  # including lanes a lowered cap retired
            offset = release_jitter_seconds(index, lanes, JITTER)
            assert 0.0 <= offset < JITTER


def test_a_retired_lane_gets_a_slot_of_its_own():
    """`lane_index % lanes` would put lane 5 of a fleet the operator cut to 4 on
    lane 1's tick. The retired lane is the one still making requests while the
    supervisor cannot see it, so it is the last one that should re-probe in
    unison with somebody."""
    assert release_jitter_seconds(5, 4, JITTER) != release_jitter_seconds(1, 4, JITTER)
    assert release_jitter_seconds(4, 4, JITTER) != release_jitter_seconds(0, 4, JITTER)


def test_the_resumed_wait_keeps_this_lane_s_offset(tmp_path):
    """The jitter's other half, and the one that vanishes quietly. The remaining
    wait is CLAMPED to what the schedule prescribes; a clamp computed from the
    un-jittered delay would clip exactly the offset, so the spread would hold on
    the fresh path and be gone after a restart."""
    config = make_config(tmp_path, lanes=4)
    lane3, _ = build_lane(config, 3)
    offset = release_jitter_seconds(3, 4, JITTER)
    assert offset > 0

    # Killed INSIDE the wait, so the stamp is still owed when the next process
    # picks it up — `_serve_rate_limit_wait` clears it once the wait is served.
    def killed_mid_wait(_seconds):
        raise KeyboardInterrupt("SIGINT while waiting out the throttle")

    lane3._sleep = killed_mid_wait
    with pytest.raises(KeyboardInterrupt):
        throttled(lane3)
    handover = lane3._store.load()
    assert handover.rate_limit_retry_not_before, "the debt outlives the process"

    second, resumed = build_lane(config, 3)
    second.state = handover
    second._store.save(handover)
    second._await_rate_limit_deadline()

    assert resumed, "the successor owes the rest of the wait"
    # The clamp is `_rate_limit_delay(streak) + jitter`; without the offset the
    # remaining wait would be clipped to BASE exactly.
    assert resumed[0] == pytest.approx(BASE + offset, abs=2.0)
    assert resumed[0] > BASE


# ---- 4. one budget for the fleet ---------------------------------------------


def seed_episode(config: AutoloopConfig, *, backoffs: int, opens_in: float) -> FleetThrottle:
    """A fleet record as an earlier process would have left it. `opens_in` is
    seconds from now to the shared deadline — negative for a window that has
    already closed."""
    now = datetime.now(timezone.utc)
    record = FleetThrottle(
        backoffs=backoffs,
        retry_not_before=(now + timedelta(seconds=opens_in)).isoformat(
            timespec="milliseconds"
        ),
        opened_at=now.isoformat(timespec="milliseconds"),
        opened_by="_lane-0",
        observations=1,
        updated_at=now.isoformat(timespec="milliseconds"),
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    throttle_store(config).save(record)
    return record


def test_the_lane_that_merely_JOINS_the_last_episode_is_the_one_that_parks(tmp_path):
    """Requirement 4, at its sharpest. Checking the budget only where an episode
    is OPENED would park the opener at the sixth episode while the other three
    lanes released, re-probed, joined a seventh and waited forever on a fleet
    that had already stopped — `max_rate_limit_backoffs` meaning something
    different in every lane."""
    config = make_config(tmp_path, lanes=4, policy=PolicyConfig(max_rate_limit_backoffs=2))
    seed_episode(config, backoffs=3, opens_in=BASE)
    joiner, taken = build_lane(config, 2)

    throttled(joiner)

    assert Phase(joiner.state.phase) is Phase.NEEDS_USER
    assert park_code(config) == "rate_limited"
    assert joiner.state.rate_limit_backoffs == 3, "the fleet's count, not this lane's"
    assert taken == [], "a parked lane does not then sit out the window"
    assert throttle_store(config).load().observations == 2, "it joined, it did not open"
    question = joiner.state.question or ""
    assert "FLEET of 4 lanes" in question
    assert "shared episodes" in question


def test_the_park_counts_episodes_however_many_lanes_observed_them(tmp_path):
    """Four lanes, a budget of two, and one limit that will not lift: the fleet
    takes TWO waits and parks on the third episode — never eight.

    Each episode is met by every lane, so the naive arithmetic would have spent
    the budget in the first window."""
    config = make_config(tmp_path, lanes=4, policy=PolicyConfig(max_rate_limit_backoffs=2))
    store = throttle_store(config)
    reference, _ = build_lane(config, 0)
    now = datetime.now(timezone.utc)
    episodes = []

    for _ in range(3):
        # One episode, met by all four lanes, then the window closes.
        for _lane in range(4):
            record = store.observe(delay_for=reference._rate_limit_delay, now=now)
        episodes.append((record.backoffs, record.observations))
        now = record.deadline() + timedelta(seconds=1)

    assert episodes == [(1, 4), (2, 4), (3, 4)], "12 observations, 3 episodes"
    allowed = [
        reference._policy.check_rate_limit_backoff_budget(backoffs).allowed
        for backoffs, _ in episodes
    ]
    assert allowed == [True, True, False], "parked on the third episode, not the first"


def test_a_lane_that_never_opened_an_episode_still_reports_the_fleet_count(tmp_path):
    """The counter an operator reads has to be the fleet's, or "6 throttled
    attempts" on a four-lane run reads as this lane's six out of twenty-four."""
    config = make_config(tmp_path, lanes=2)
    seed_episode(config, backoffs=4, opens_in=BASE)
    joiner, _ = build_lane(config, 1)

    throttled(joiner)

    assert joiner.state.rate_limit_backoffs == 4
    logged = entries(config, "rate_limited")[-1]
    assert logged["backoffs"] == 4
    assert logged["fleet_episode"] == 4 and logged["fleet_observations"] == 2


# ---- 5. durability, and the poison pill --------------------------------------


def test_the_episode_outlives_the_process(tmp_path):
    """The per-lane stamp is durable already; the SHARED one has to be too, or a
    restart puts every lane back to zero with no wait outstanding and the fleet
    is ready to hammer again."""
    config = make_config(tmp_path, lanes=2)
    first, _ = build_lane(config, 0)
    throttled(first)
    opened = throttle_store(config).load()

    # A second process over the same state directory, in another lane.
    second, taken = build_lane(config, 1)
    throttled(second)

    record = throttle_store(config).load()
    assert record.backoffs == 1 and record.observations == 2
    assert record.retry_not_before == opened.retry_not_before
    assert second.state.rate_limit_backoffs == 1
    assert taken and taken[0] < BASE + JITTER + 1, "the REMAINDER, not a fresh window"


def test_a_throttle_long_after_the_last_window_starts_a_fresh_streak(tmp_path):
    """The poison pill this record could otherwise become. Nothing sweeps the
    file: a fleet that parked at the sixth episode leaves `backoffs = 6` and a
    long-expired deadline behind it, and inheriting that unconditionally would
    make the next run's FIRST throttle — hours later, about a different limit —
    open episode 7 and park on the spot.

    The escalation's premise is that the waits are CONSECUTIVE, and two
    throttles an hour apart are not."""
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    store = throttle_store(config)
    now = datetime.now(timezone.utc)
    stale = FleetThrottle(
        backoffs=6,
        retry_not_before=(now - timedelta(hours=4)).isoformat(timespec="milliseconds"),
        opened_at=(now - timedelta(hours=4)).isoformat(timespec="milliseconds"),
        opened_by="_lane-0",
        observations=3,
        updated_at=(now - timedelta(hours=4)).isoformat(timespec="milliseconds"),
    )
    store.save(stale)

    record = store.observe(delay_for=lane0._rate_limit_delay, now=now)

    assert record.backoffs == 1, "a fresh incident starts fresh"
    assert record.observations == 1


def test_a_zero_back_off_still_coalesces_and_still_parks(tmp_path):
    """The degenerate setting both floors exist for, and it is a config an
    operator may write. With `rate_limit_backoff_seconds = 0` an unfloored
    window would close in the instant it opened — so four lanes meeting one
    limit together would open FOUR episodes, the exact miscount this record
    removes — and an unfloored grace would restart the streak at 1 every time,
    so the fleet would never reach `max_rate_limit_backoffs` and never park.

    The lanes still wait ZERO: the floors bound the coalescing window, never the
    wait, which `_handle_rate_limited` clamps to the configured schedule."""
    config = make_config(
        tmp_path, lanes=4, base=0.0, ceiling=0.0, rate_limit_release_jitter_seconds=0.0
    )
    lanes = [build_lane(config, index) for index in range(4)]

    for lane, _ in lanes:
        throttled(lane)

    record = throttle_store(config).load()
    assert (record.backoffs, record.observations) == (1, 4), "one limit, one episode"
    assert all(taken == [] for _, taken in lanes), "and no wait was invented"

    # And the streak still escalates rather than restarting, so the budget is
    # still reachable: the grace floor is what carries it across a window that
    # is already closed by the time the next throttle arrives.
    store = throttle_store(config)
    later = record.deadline() + timedelta(seconds=2)
    assert store.observe(delay_for=lanes[0][0]._rate_limit_delay, now=later).backoffs == 2


def test_a_throttle_just_after_the_window_closed_is_still_consecutive(tmp_path):
    """The other side of the same rule, and the one that matters in production:
    a lane releases at the deadline plus its jitter, re-probes, and is throttled
    again seconds later. That IS the same incident, and reading it as a fresh
    one would flatten the escalation into a fixed 60-second retry that never
    parks."""
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    store = throttle_store(config)
    now = datetime.now(timezone.utc)
    opened = store.observe(delay_for=lane0._rate_limit_delay, now=now)

    again = store.observe(
        delay_for=lane0._rate_limit_delay,
        now=opened.deadline() + timedelta(seconds=3),
    )

    assert again.backoffs == 2
    assert again.observations == 1, "a new episode, not a join"


def test_a_completed_step_in_a_throttled_lane_ends_the_fleet_episode(tmp_path):
    """The only honest evidence that the limit has lifted is a step that
    COMPLETED, and it is evidence about the account — so it ends the FLEET's
    episode, not merely this lane's counter."""
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    throttled(lane0)
    assert fleet_throttle_file(config.state_dir).exists()

    lane0.state.phase = Phase.READY.value
    lane0._step = lambda phase: setattr(lane0.state, "phase", Phase.STOPPED.value)
    lane0.run(max_steps=1)

    assert lane0.state.rate_limit_backoffs == 0
    assert not fleet_throttle_file(config.state_dir).exists(), "the episode is over"


def test_a_lane_that_was_never_throttled_does_not_end_the_episode(tmp_path):
    """The fail-open the reset rule is one line away from. "Any completed step
    in any lane clears it" would let a lane doing purely local work — validation,
    git, an agent — reset a streak it never observed, so the escalation would
    never reach `max_rate_limit_backoffs` and the fleet would never park."""
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    throttled(lane0)
    untouched, _ = build_lane(config, 1)
    assert untouched.state.rate_limit_backoffs == 0

    untouched.state.phase = Phase.READY.value
    untouched._step = lambda phase: setattr(
        untouched.state, "phase", Phase.STOPPED.value
    )
    untouched.run(max_steps=1)

    assert fleet_throttle_file(config.state_dir).exists(), "somebody else's episode"
    assert throttle_store(config).load().backoffs == 1


def test_an_unreadable_record_parks_the_lane_rather_than_backing_off_alone(tmp_path):
    """`_handle_rate_limited` runs inside `run`'s `except RateLimitedError:`
    clause, where a raise is caught by none of the sibling handlers — including
    the `StateError` one, which exists because two runs once left by traceback
    with no park and no blocker.

    And the alternative to parking is worse than a traceback, not better:
    falling back to this lane's own counter would put the fleet back on N
    independent back-offs at exactly the moment its shared record is in trouble,
    with nothing saying so."""
    config = make_config(tmp_path, lanes=2)
    lane1, taken = build_lane(config, 1)
    fleet_throttle_file(config.state_dir).write_text('{"backoffs": "six"}', encoding="utf-8")

    throttled(lane1)

    assert Phase(lane1.state.phase) is Phase.NEEDS_USER
    assert park_code(config) == "fleet_throttle_unreadable"
    assert lane1.state.rate_limit_backoffs == 0, "no private back-off was started"
    assert taken == [], "and nothing was waited out on a counter nobody can read"
    assert str(fleet_throttle_file(config.state_dir)) in (lane1.state.question or "")


def test_the_record_refuses_shapes_no_writer_produces(tmp_path):
    """Types are checked rather than merely unpacked, for
    `StopRepetitionStore.load`'s reason: `backoffs` is compared against a budget
    and fed to the delay schedule, and `True` is an `int` in Python."""
    store = FleetThrottleStore(tmp_path / "fleet_throttle.json")
    good = dict(
        backoffs=1,
        retry_not_before=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        opened_at="x",
        opened_by="_lane-0",
        observations=1,
        updated_at="x",
    )
    for bad in (
        {"backoffs": True},
        {"backoffs": "3"},
        {"backoffs": 0},
        {"observations": -1},
        {"retry_not_before": "not a timestamp"},
        {"retry_not_before": 17},
        {"opened_by": None},
    ):
        store.path.write_text(json.dumps({**good, **bad}), encoding="utf-8")
        with pytest.raises(StateCorruptError):
            store.load()
    # An unknown key is refused too — a record this build cannot fully describe
    # is not one it may act on.
    store.path.write_text(json.dumps({**good, "surprise": 1}), encoding="utf-8")
    with pytest.raises(StateCorruptError):
        store.load()

    store.path.write_text(json.dumps(good), encoding="utf-8")
    assert store.load().backoffs == 1


# ---- 6. at one lane, nothing exists ------------------------------------------


def test_at_one_lane_no_fleet_record_is_ever_written(tmp_path):
    """The acceptance criterion made structural, in the form conc-05 uses for
    lane 0's state file: at one lane no new file appears under the state dir."""
    config = make_config(tmp_path, lanes=1)
    lane, taken = build_lane(config, 0)

    for _ in range(3):
        throttled(lane)

    assert not fleet_throttle_file(config.state_dir).exists()
    assert [
        p.name
        for p in config.state_dir.rglob("*")
        if "throttle" in p.name or "lanes" == p.name
    ] == [], "no fleet-scoped file and no lanes directory"
    assert taken == [60.0, 120.0, 240.0], "today's sequence, unchanged"
    assert lane.state.rate_limit_backoffs == 3


def test_at_one_lane_the_persisted_stamp_is_todays(tmp_path):
    """The stamp is `now + delay` with no offset of any kind, and the wait it
    resumes into is the un-jittered schedule."""
    config = make_config(tmp_path, lanes=1)
    lane, _ = build_lane(config, 0)

    opened = datetime.now(timezone.utc)
    stamp = throttled_stamp(lane)

    assert lane._rate_limit_release_jitter() == 0.0
    assert lane._fleet_throttle_store() is None
    # `now + 60`, with nothing added to it: the single-lane stamp has no offset.
    assert (stamp - opened).total_seconds() == pytest.approx(BASE, abs=0.5)
    logged = entries(config, "rate_limited")[-1]
    assert logged["backoff_seconds"] == 60.0
    assert "fleet_episode" not in logged, "no fleet keys on a single-lane run"


class _ScriptedClock:
    """`datetime` with a scripted `now()`, so a test can see WHICH clock read a
    stamp was computed from rather than only that it landed near the right
    second.

    Every other attribute is the real class (`__getattr__`), so
    `fromisoformat` — the only other `datetime` call `orchestrator` makes — is
    untouched. Substituted for one call and undone by `monkeypatch`.
    """

    def __init__(self, first: datetime, later: datetime):
        self._moments = [first, later]
        self.reads: list[datetime] = []

    def now(self, tz=None):
        moment = self._moments[min(len(self.reads), len(self._moments) - 1)]
        self.reads.append(moment)
        return moment

    def __getattr__(self, name):
        return getattr(datetime, name)


def test_at_one_lane_the_stamp_is_measured_at_the_persist_site(monkeypatch, tmp_path):
    """The single-lane stamp is `<the clock read where the state is saved> +
    delay` — the call site this handler has always used — and NOT the earlier
    read the fleet record needs.

    `test_at_one_lane_the_persisted_stamp_is_todays` above cannot see this and
    is not evidence for it: on a real clock the two reads are microseconds
    apart and that test asserts to within half a second, so it passes either
    way. A scripted clock separates them by an hour, which is what makes the two
    call sites give visibly different answers.
    """
    config = make_config(tmp_path, lanes=1)
    lane, _ = build_lane(config, 0)
    episode_read = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    persist_read = episode_read + timedelta(hours=1)
    clock = _ScriptedClock(episode_read, persist_read)
    monkeypatch.setattr(orchestrator_module, "datetime", clock)

    stamp = throttled_stamp(lane)

    # Two reads, in this order: the one `_join_fleet_throttle` is handed (a
    # no-op at one lane) and the one the stamp is measured from.
    assert len(clock.reads) == 2
    assert stamp == persist_read + timedelta(seconds=BASE)


def test_on_a_fleet_the_stamp_is_measured_from_the_episode_read(monkeypatch, tmp_path):
    """The other half of the same rule, and the reason it is a conditional
    rather than a straight revert: a fleet lane's `delay` is the REMAINDER of the
    shared window measured against the episode read, so a second clock read at
    the persist site would count the elapsed time twice and put this lane's
    stamp past the deadline it just joined.

    Asserted as an equality against the fleet record's own deadline plus this
    lane's offset — the invariant `_await_rate_limit_deadline` resumes on.
    """
    config = make_config(tmp_path, lanes=2)
    lane, _ = build_lane(config, 1)
    episode_read = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    clock = _ScriptedClock(episode_read, episode_read + timedelta(hours=1))
    monkeypatch.setattr(orchestrator_module, "datetime", clock)

    stamp = throttled_stamp(lane)

    record = throttle_store(config).load()
    offset = release_jitter_seconds(1, 2, JITTER)
    assert stamp == record.deadline() + timedelta(seconds=offset)
    assert stamp == episode_read + timedelta(seconds=BASE + offset)


def test_at_one_lane_the_park_text_is_unchanged(tmp_path):
    """The fleet clause is appended only for a fleet, so a single-lane park is
    byte-identical to the one `test_transport_fault_recovery.py` pins."""
    config = make_config(
        tmp_path, lanes=1, policy=PolicyConfig(max_rate_limit_backoffs=1)
    )
    lane, _ = build_lane(config, 0)

    throttled(lane)
    throttled(lane)

    question = lane.state.question or ""
    assert Phase(lane.state.phase) is Phase.NEEDS_USER
    assert "FLEET of" not in question and "shared episodes" not in question
    assert "across 2 throttled attempts" in question


def test_at_one_lane_the_supervisor_holds_nothing_for_a_throttle(tmp_path):
    """`from_config` wires no throttle store below two lanes, so the single-lane
    plan cannot acquire a hold word that did not exist before this task — even
    with a record sitting in the state dir from a fleet the operator scaled
    down."""
    config = make_config(tmp_path, lanes=1)
    registry = registry_of(a_task("t1"), a_task("t2"))
    seed_episode(config, backoffs=3, opens_in=BASE)

    plan = FleetSupervisor.from_config(config).plan(registry)

    assert [task.id for task in plan.admitted] == ["t1"]
    assert plan.hold_reason("t2") == HOLD_AT_CAP
    assert not plan.fleet_throttled


# ---- the setting, and what `health` shows -------------------------------------


def write_config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nworkers_root = "{tmp_path / "w"}"\n\n' + body, encoding="utf-8"
    )
    return path


def test_the_release_spread_setting_loads_and_is_bounded(tmp_path):
    loaded = load_config(
        write_config(
            tmp_path, "[concurrency]\nlanes = 2\nrate_limit_release_jitter_seconds = 2\n"
        )
    )
    assert loaded.concurrency.rate_limit_release_jitter_seconds == 2.0
    assert isinstance(loaded.concurrency.rate_limit_release_jitter_seconds, float)

    for bad in ("true", '"2"', "-1", str(MAX_RELEASE_JITTER_SECONDS + 1)):
        with pytest.raises(ConfigError):
            load_config(
                write_config(
                    tmp_path,
                    f"[concurrency]\nrate_limit_release_jitter_seconds = {bad}\n",
                )
            )


def test_the_spread_survives_a_section_that_does_not_name_lanes(tmp_path):
    """The early return for an absent `lanes` must not drop a spread the
    operator did name — a setting that loads and is then discarded is worse than
    one that was refused."""
    loaded = load_config(
        write_config(tmp_path, "[concurrency]\nrate_limit_release_jitter_seconds = 1.5\n")
    )
    assert loaded.concurrency.lanes == 1
    assert loaded.concurrency.rate_limit_release_jitter_seconds == 1.5


def test_health_reports_the_shared_window_and_only_above_one_lane(tmp_path):
    """A fleet holding every lane for ten minutes and a fleet with nothing to do
    must not read identically from outside."""
    config = make_config(tmp_path, lanes=3)
    lane0, _ = build_lane(config, 0)
    lane1, _ = build_lane(config, 1)
    throttled(lane0)
    throttled(lane1)

    view = health_module.fleet_throttle_view(config)

    assert view is not None
    assert view.open and view.backoffs == 1 and view.observations == 2
    assert view.opened_by == lane0.lane_id
    assert 0 < view.seconds_remaining <= BASE
    assert view.release_spread_max_seconds == JITTER
    assert view.unreadable == ""
    assert view.retry_not_before == throttle_store(config).load().retry_not_before

    single = dataclasses.replace(config, concurrency=ConcurrencyConfig(lanes=1))
    assert health_module.fleet_throttle_view(single) is None


def test_health_says_so_when_the_record_cannot_be_read(tmp_path):
    """`health` is what a monitor runs on a schedule against a loop it is not
    part of: a reader that died on a corrupt record would report nothing at all
    about a fleet that is, at that moment, admitting nothing."""
    config = make_config(tmp_path, lanes=2)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    fleet_throttle_file(config.state_dir).write_text("]", encoding="utf-8")

    view = health_module.fleet_throttle_view(config)

    assert view is not None and view.unreadable
    assert view.open, "nothing known is not permission"


def test_health_carries_no_fleet_throttle_when_there_is_none(tmp_path):
    """Additive: a reader gains a key and loses nothing, and the ordinary state
    is `null`."""
    config = make_config(tmp_path, lanes=2)
    build_lane(config, 0)

    verdict = health_module.check(config, agent_probe=lambda: False)

    assert verdict.fleet_throttle is None
    assert '"fleet_throttle": null' in verdict.to_json()


# ---- the empty queue: the tick with nothing to hold ---------------------------
#
# Requirement 2 reaches `plan` through the QUEUE — every READY task is held
# `fleet_rate_limited` while the window is open — and there is one shape of it
# no queue can carry. `cli._run_continuous` sleeps on a plan when the fleet is
# draining, or when the queue has entries and every one of them is held; an
# EMPTY queue holds nothing, so that reads as "no fleet objection" and the
# iteration falls through to `_select_and_kickoff`, which on a changed
# repository fingerprint opens an AUDIT session: one request against the
# allowance the fleet is waiting out, and a silent one, since `_log_fleet_hold`
# sits on the branch that was skipped.
#
# The term that answers it is the plan's own `fleet_throttled`, beside the
# drain's, and the five tests below are its parts: the supervisor's answer on
# that tick, the LOOP's use of it — driven through the real `_run_continuous`,
# because "no session is opened" is a claim about the caller and not about the
# supervisor it asks — the same loop over a record nobody can READ, where `held`
# is empty for the second reason and the fall-through would have been the alarm
# that never fires, the same loop at ONE lane, where the term is never evaluated
# at all, and the bound that still holds if a round reaches an open window from
# somewhere this gate cannot see.


class StopTheLoop(Exception):
    """Ends `_run_continuous` from inside a fake sleep or a fake selection —
    which iteration it is reached on is half of the assertion."""


def continuous_args() -> argparse.Namespace:
    return argparse.Namespace(config=None, continuous=True, null_executor=False)


def test_the_plan_reports_the_window_with_nothing_in_the_queue(tmp_path):
    """The tick the caller used to fall through on, asked of the supervisor
    directly.

    Nothing in `plan` had to change for the caller's term to work: `admitted` is
    empty, `held` is empty BECAUSE THERE IS NOTHING TO HOLD — not because
    anything was allowed — and `fleet_throttled` carries the shared deadline on
    the same tick. An empty `held` is precisely what a condition reading `held`
    alone misreads as "no fleet objection", so it is asserted as the distinct
    fact it is rather than folded into "nothing was admitted".
    """
    config = make_config(tmp_path, lanes=2)
    empty = registry_of()
    lane0, _ = build_lane(config, 0)
    throttled(lane0)

    plan = FleetSupervisor.from_config(config).plan(empty)

    assert plan.admitted == ()
    assert plan.held == (), "an empty queue holds nothing — the shape at issue"
    assert plan.fleet_throttled, "and the answer is on the plan all the same"
    assert plan.throttled_until == throttle_store(config).load().retry_not_before
    assert HOLD_RATE_LIMITED in plan.describe(), "a reader is told which it is"


def test_a_throttled_fleet_with_an_empty_queue_opens_no_session(
    tmp_path, monkeypatch
):
    """The LOOP half, driven through the real `cli._run_continuous` — and two
    iterations, because the second is what makes this a gate rather than a wedge.

    Iteration 1: the window is open and the queue is EMPTY, so nothing is held
    and a condition reading `held` alone is False. Reaching `_select_and_kickoff`
    at all is the failure — it is the only door to `_start_new_session` — so the
    session file and the audit fingerprint are asserted absent afterwards as the
    durable half of "no request was made", and the hold is asserted to have been
    LOGGED, which the skipped branch never did.

    Iteration 2: the window expires inside the fake poll — the drain test's own
    device, matched by DURATION so an unrelated internal sleep cannot expire it a
    step early — and the selection is reached on the very next tick. The poll
    count is bounded so a gate that never lifts fails this test instead of
    hanging the suite on a fake sleep that returns instantly.
    """
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry())
    # Seeded rather than driven through a lane, so the fleet is IDLE while the
    # window is open: `_handle_rate_limited` leaves a busy state file behind it,
    # and a hold measured with one of those in the fleet could be the cap
    # wearing the throttle's name.
    window = seed_episode(config, backoffs=1, opens_in=BASE)
    events: list[str] = []

    def poll(seconds):
        if seconds != cli.CONTINUOUS_POLL_SECONDS:
            return
        events.append("held")
        if len(events) > 3:
            raise StopTheLoop()  # the window never lifted — see the assert below
        # The window expiring between two iterations, which is the drain test's
        # own device for changing the fleet inside the fake poll.
        seed_episode(config, backoffs=1, opens_in=-1.0)

    def select(cfg, store, registry):
        events.append("session")
        raise StopTheLoop()

    monkeypatch.setattr(cli.time, "sleep", poll)
    monkeypatch.setattr(cli, "_select_and_kickoff", select)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)

    assert events == ["held", "session"], (
        "a session was opened inside the fleet's own throttle window"
        if events[:1] == ["session"]
        else "the hold never lifted after the shared deadline passed"
    )
    assert not config.state_file.exists(), "a session was opened while throttled"
    assert not config.continuous_fingerprint_file.exists(), "an audit was started"
    logged = entries(config, "fleet_hold")
    assert len(logged) == 1, "the throttled tick has to be legible, not silent"
    assert logged[0]["held"] == "" and logged[0]["draining"] is False, (
        "neither of the reasons that already stopped admission — the shape at issue"
    )
    assert HOLD_RATE_LIMITED in logged[0]["detail"], "and it says which it is"
    assert window.retry_not_before in logged[0]["detail"], "with the shared deadline"
    assert not config.executions_dir.exists(), "the held tick charged no attempt"


def test_a_record_nobody_can_read_holds_the_loop_and_not_only_the_plan(
    tmp_path, monkeypatch
):
    """The same fail-open one level up, and the empty queue is what makes it
    worth its own test.

    `plan` answers `FLEET_THROTTLE_UNREADABLE` rather than `""` for a record it
    cannot read, so `fleet_throttled` is TRUE and the loop holds on it exactly as
    it holds on an open window. With nothing in the queue `held` is empty in BOTH
    states, so a caller reading `held` alone opens a session precisely when
    nothing can tell whether the account is throttled — the alarm that never
    fires. The hold is asserted to name the record as the reason, because "the
    window is open" and "nobody can tell" are the two answers this record exists
    to keep apart.
    """
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry())
    config.state_dir.mkdir(parents=True, exist_ok=True)
    fleet_throttle_file(config.state_dir).write_text("{not json", encoding="utf-8")

    def poll(seconds):
        if seconds == cli.CONTINUOUS_POLL_SECONDS:
            raise StopTheLoop()  # one tick is the whole claim: it did not admit

    monkeypatch.setattr(cli.time, "sleep", poll)
    monkeypatch.setattr(
        cli,
        "_select_and_kickoff",
        lambda *a, **k: pytest.fail("an unreadable record is not permission"),
    )

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)

    logged = entries(config, "fleet_hold")
    assert len(logged) == 1 and HOLD_RATE_LIMITED in logged[0]["detail"]
    assert FLEET_THROTTLE_UNREADABLE in logged[0]["detail"], "which of the two it is"
    assert not config.state_file.exists() and not config.executions_dir.exists()


def test_at_one_lane_the_same_record_stops_nothing(tmp_path, monkeypatch):
    """The single-lane half of the same term, driven through the same loop.

    `cli._fleet_plan` answers `None` below two lanes, so no plan is computed, no
    `fleet_throttled` is read and the new term is never evaluated: a single-lane
    deployment reaches `_select_and_kickoff` on the FIRST iteration holding the
    very record that stops a fleet of two, and its back-off stays entirely
    `LoopState`'s own two fields. The seeded record and the empty queue are the
    ones above, so `[concurrency] lanes` is the only difference between the two
    tests — which is what makes this evidence about the gate rather than about
    the fixture.
    """
    config = make_config(tmp_path, lanes=1)
    TaskStore(config.tasks_file).save(TaskRegistry())
    seed_episode(config, backoffs=1, opens_in=BASE)
    events: list[str] = []

    def poll(seconds):
        # By duration, so an unrelated internal sleep is not read as a fleet
        # hold — the same matching rule the two-iteration test above uses.
        if seconds == cli.CONTINUOUS_POLL_SECONDS:
            events.append("held")

    def select(cfg, store, registry):
        events.append("session")
        raise StopTheLoop()

    monkeypatch.setattr(cli.time, "sleep", poll)
    monkeypatch.setattr(cli, "_select_and_kickoff", select)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)

    assert events == ["session"], "a fleet record held a single-lane loop"
    assert entries(config, "fleet_hold") == [], "and nothing was logged as one"


def test_an_audit_round_that_meets_the_limit_joins_the_open_episode(tmp_path):
    """The bound that holds when a round reaches the limit from somewhere the
    admission gate cannot see, and the reason THE CLAIM does not rest on that
    gate alone.

    Admission is refused a tick at a time, so a round can still be in flight
    inside an open window: a lane admitted in the instant before the window
    opened, or one an operator's lowered cap cut out of the fleet — which no
    supervisor sees at all (`HOLD_LANE_RETIRED`). The shape asserted here is the
    hardest of those, a session that names NO task (`_start_new_session` opens on
    the audit kickoff), because it is the one a task-keyed rule would miss.

    `_join_fleet_throttle` is keyed on the shared record alone — nothing in it
    reads the task, or the absence of one — so that observation JOINS the open
    window like any other lane's. `observations` grows, `backoffs` does not, the
    shared deadline is not extended, and one limit is still one episode. Nor is
    it silent: the round writes the ordinary `rate_limited` entry with the
    fleet's own episode and observation counts on it.
    """
    config = make_config(tmp_path, lanes=2)
    working, _ = build_lane(config, 0)
    working.state.current_task = {"task_id": "t1", "title": "task t1"}
    working._store.save(working.state)
    throttled(working)
    opened = throttle_store(config).load()

    # Lane 1 at a clean boundary with an empty queue: the audit session, which
    # names no task — the discriminator, asserted rather than assumed.
    audit_lane, taken = build_lane(config, 1)
    assert session_task_id(working.state) == "t1"
    assert session_task_id(audit_lane.state) is None, "an audit names no task"

    throttled(audit_lane)

    record = throttle_store(config).load()
    assert (opened.backoffs, record.backoffs) == (1, 1), "one limit, one episode"
    assert record.observations == 2, "the in-flight round is an observation"
    assert record.retry_not_before == opened.retry_not_before, "and extends nothing"
    assert audit_lane.state.rate_limit_backoffs == 1
    assert taken and taken[0] == pytest.approx(
        BASE + release_jitter_seconds(1, 2, JITTER), abs=2.0
    ), "it waits out the REMAINDER of the open window, not a fresh copy"
    joined = entries(config, "rate_limited")[-1]
    assert joined["fleet_episode"] == 1 and joined["fleet_observations"] == 2


# ---- ending an episode: which one, exactly ------------------------------------
#
# The half of the record's lifecycle the sections above do not reach. Opening and
# joining are decided under the mutex; ENDING one was not — a completing lane
# unlinked the file whatever it held. That is fine while the record can only ever
# be the episode that lane observed, and it stops being fine the moment another
# lane can open the next one in between, which is the ordinary shape of a fleet:
# lane 0's retry succeeds, lane 1 meets the limit again, and lane 0's completed
# step then deletes a live deadline and a counter that had just escalated.
#
# The identity is `FleetThrottle.episode_id`, mirrored into the observing lane's
# own state file so it survives the process, and the clear compares against it
# under the same mutex `observe` takes.


def reopen_lane(config: AutoloopConfig, lane_index: int = 0):
    """Another PROCESS over the same lane: everything is rebuilt, and the state
    is LOADED from that lane's file rather than made fresh.

    `build_lane` writes a new `LoopState` over whatever was there, which is what
    every other test here wants and is exactly wrong for a durability claim.
    """
    store = StateStore(lane_state_file(config.state_dir, lane_index))
    state = store.load()
    assert state is not None, "nothing was persisted for this lane"
    task_store = TaskStore(config.tasks_file)
    registry = task_store.load() or TaskRegistry()
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: None,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        lane_index=lane_index,
    )
    orch._sleep = lambda _seconds: None
    return orch


def completes_a_step(orch: Orchestrator) -> None:
    """Drive one step that finishes, which is `run`'s only evidence that the
    account is serving requests again — and therefore the only thing that ends an
    episode."""
    orch.state.phase = Phase.READY.value
    orch._step = lambda phase: setattr(orch.state, "phase", Phase.STOPPED.value)
    orch.run(max_steps=1)


def test_clear_removes_only_the_episode_it_names(tmp_path):
    """The store's side of it, on its own: a clear naming an episode that is no
    longer the stored one changes nothing and says so."""
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    store = throttle_store(config)
    opened = store.observe(delay_for=lane0._rate_limit_delay, lane_id="_lane-0")
    assert opened.episode_id, "an episode that opens is named"

    assert store.clear("episode-nobody-opened") is False
    survivor = store.load()
    assert survivor is not None and survivor.episode_id == opened.episode_id

    assert store.clear(opened.episode_id) is True
    assert store.load() is None
    assert store.clear(opened.episode_id) is False, "and it is idempotent"


def test_a_newer_episode_survives_the_older_lane_s_completed_step(tmp_path):
    """THE RACE THE UNCONDITIONAL CLEAR LOST, and the reason the comparison is
    not decoration.

    Lane 0 waits out episode 1 and its retry is served. Before its step
    completes, lane 1 meets the limit again — the window has closed, so that is
    episode 2, at `previous + 1`, with a live deadline the whole fleet is now
    inside. An unlink of "the record" would erase that deadline and put the
    streak back to zero: admission reopens mid-window, `_rate_limit_delay` starts
    over at 60s, and `max_rate_limit_backoffs` stops being reachable — the guard
    switching itself off, with nothing saying so.
    """
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    throttled(lane0)
    store = throttle_store(config)
    first = store.load()
    assert lane0.state.fleet_throttle_episode == first.episode_id

    lane1, _ = build_lane(config, 1)
    second = store.observe(
        delay_for=lane1._rate_limit_delay,
        lane_id="_lane-1",
        now=first.deadline() + timedelta(seconds=3),
    )
    assert second.backoffs == 2, "a new episode, and the streak escalated"
    assert second.episode_id != first.episode_id

    completes_a_step(lane0)

    surviving = store.load()
    assert surviving is not None, "episode 2 was erased by a lane that never saw it"
    assert surviving.episode_id == second.episode_id
    assert surviving.backoffs == 2, "and the escalated counter is still there"
    assert surviving.retry_not_before == second.retry_not_before
    # The lane's OWN reset is unaffected — it really was served.
    assert lane0.state.rate_limit_backoffs == 0
    assert lane0.state.fleet_throttle_episode == ""
    skipped = entries(config, "fleet_throttle_clear_skipped")
    assert skipped and skipped[-1]["episode_id"] == first.episode_id, "not silent"


def test_a_fresh_episode_one_is_not_the_old_episode_one(tmp_path):
    """Why the identity cannot be `backoffs`, which is the cheap version of this
    fix and is wrong.

    A throttle arriving long after the last window closed starts a FRESH streak,
    so it is episode 1 again — the same number the earlier episode carried. A
    lane comparing on that number would delete the new one.
    """
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    store = throttle_store(config)
    now = datetime.now(timezone.utc)
    first = store.observe(delay_for=lane0._rate_limit_delay, lane_id="_lane-0", now=now)
    fresh = store.observe(
        delay_for=lane0._rate_limit_delay,
        lane_id="_lane-1",
        now=now + timedelta(hours=4),
    )

    assert (first.backoffs, fresh.backoffs) == (1, 1), "the same number"
    assert fresh.episode_id != first.episode_id, "and not the same episode"
    assert store.clear(first.episode_id) is False
    assert store.load().episode_id == fresh.episode_id


def test_every_lane_in_one_episode_names_the_same_id(tmp_path):
    """One episode is ONE id however many lanes met it — the join copies the
    opener's — so any lane that was inside it may end it, and none of them can
    end anything else."""
    config = make_config(tmp_path, lanes=3)
    lanes = [build_lane(config, index)[0] for index in range(3)]

    for lane in lanes:
        throttled(lane)

    record = throttle_store(config).load()
    assert (record.backoffs, record.observations) == (1, 3)
    assert {lane.state.fleet_throttle_episode for lane in lanes} == {record.episode_id}

    completes_a_step(lanes[2])
    assert throttle_store(config).load() is None, "a joiner may end its own episode"


def test_the_observed_episode_id_outlives_the_process(tmp_path):
    """Persisted beside the counter, and for the counter's reason: the episode
    outlives the process that met it.

    Held only in memory, a lane killed mid-back-off would come back unable to
    name what it observed, so its completed step would end nothing and the record
    would sit there with the streak on it — the next throttle inside the grace
    escalating from a limit that had in fact lifted.
    """
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    throttled(lane0)
    opened = throttle_store(config).load()

    resumed = reopen_lane(config, 0)
    assert resumed.state.fleet_throttle_episode == opened.episode_id
    completes_a_step(resumed)

    assert throttle_store(config).load() is None, "the episode it observed ended"


def test_a_lane_that_cannot_name_an_episode_clears_nothing(tmp_path):
    """The fail-closed direction of the empty id, which is what a state file
    written before the field existed carries.

    "I cannot name what I was inside" is not evidence about any record, so it
    ends none. The cost is bounded and paid by the deadline: the window still
    expires on its own.
    """
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    throttled(lane0)
    lane0.state.fleet_throttle_episode = ""

    completes_a_step(lane0)

    assert fleet_throttle_file(config.state_dir).exists(), "no id, no clear"
    assert lane0.state.rate_limit_backoffs == 0, "the lane's own reset still happens"


def test_a_record_nobody_can_read_is_not_deleted_by_a_completed_step(tmp_path):
    """The same rule the rest of this store keeps, on the one path that used to
    break it: an unconditional unlink removed a record it had never read.

    A record nobody can read is not a fleet that is free — it is what holds
    admission (`HOLD_RATE_LIMITED`) and what `health` reports as `unreadable`.
    Deleting it on the way past would be that alarm switched off by the loop
    itself. It is refused and logged instead, so an operator has both the file
    and the reason.
    """
    config = make_config(tmp_path, lanes=2)
    lane0, _ = build_lane(config, 0)
    throttled(lane0)
    path = fleet_throttle_file(config.state_dir)
    path.write_text("{ not json at all", encoding="utf-8")

    completes_a_step(lane0)

    assert path.exists(), "a record nobody can read is not a fleet that is free"
    assert entries(config, "fleet_throttle_clear_failed"), "and it is not silent"
    assert lane0.state.rate_limit_backoffs == 0, "the lane itself still recovered"


def test_at_one_lane_a_completed_step_still_reads_no_record_at_all(tmp_path):
    """The `lanes = 1` criterion on this path too: the id is never written, the
    store is never built, and a stray record in the state directory is neither
    read nor removed."""
    config = make_config(tmp_path, lanes=1)
    lane, _ = build_lane(config, 0)
    throttled(lane)
    assert lane.state.fleet_throttle_episode == ""
    # A record left behind by a multi-lane run of the same directory.
    stray = FleetThrottle(
        backoffs=3,
        retry_not_before=(
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(timespec="milliseconds"),
        opened_at="x",
        opened_by="_lane-1",
        observations=2,
        updated_at="x",
        episode_id="somebody-else-s-episode",
    )
    throttle_store(config).save(stray)

    completes_a_step(lane)

    assert lane.state.rate_limit_backoffs == 0
    assert throttle_store(config).load().episode_id == "somebody-else-s-episode"
