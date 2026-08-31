"""policy-01: `policy.max_policy_denials` must bound the policy denials raised
INSIDE a dispatch, not only the ones raised at the policy gate above it.

THE INCIDENT (2026-08-31, 20:41-20:55 UTC). The reviewer answered every packet
with a bare `push` — no postcommit binding, no changeset — and `_dispatch`
correctly refused each one as `legacy_git_path_retired`. `max_policy_denials`
was 3. It denied 71 times in 14 minutes, ~5 iterations a minute against a normal
one per 45-minute round, and ended only when the reviewer changed its own mind.
Every liveness signal stayed green throughout — lock held, heartbeat fresh,
phase changing, child process live — because a livelock is alive.

THE CAUSE was one statement's position, not the persistence failure it looked
like. `_step_executing` cleared `state.policy_denials` the moment
`authorize_directive` ALLOWED a directive, immediately before `_dispatch`. A
bare `push` passes that gate (unprotected branch, `allow_push` on) and passes
`verify_review`; it is refused LATER, inside the dispatch, by the retired
legacy-git branch — which calls `_handle_policy_denial` on a counter that had
just been zeroed. The counter therefore oscillated 0 -> 1 -> 0 forever and
`check_denial_budget` was asked about 1, seventy-one times.

THE DISCRIMINATING PAIR, and why the mechanism read as correct: `ask_user` is
refused by `authorize_directive` itself, ABOVE the clear, so
`test_orchestrator.py::test_repeated_ask_user_stops_the_run_and_never_parks` has
always passed. Same handler, same counter, same budget — only the side of the
clear differs. Around forty denial sites sit on the losing side of it
(`_dispatch_recut`, `_dispatch_executor`, every push-time check), so the hole
was never specific to the one code the incident happened to emit.

Everything here is driven through `run()` or the real phase machine —
`_handle_policy_denial` is never called directly, because a test that calls it
directly is exactly the test that passed all through the incident.
"""

from __future__ import annotations

import json

import pytest

from autoloop.blockers import BlockerStore
from autoloop.config import AutonomyConfig
from autoloop.conversation import register_provider
from autoloop.policy import PolicyConfig
from autoloop.state import Phase

# Sibling test module, importable because pytest's prepend import mode puts this
# directory on `sys.path` — the same borrowing `test_rounds_and_restart.py` and
# `test_test_selection.py` already do for `build`.
from test_orchestrator import (
    BROWSER_PROVIDER,
    approval,
    build,
    stop_block,
)

#: The refusal the incident repeated, and the only one a bare `push` can draw.
LEGACY = "legacy_git_path_retired"

#: Its prose, which is what a terminal's operator-facing text carries (the code
#: goes to the transcript and to the blocker's `detail`).
LEGACY_REASON = "direct commit/push is no longer supported"

#: `max_policy_denials` defaults to 3 and `check_denial_budget` denies at
#: `> max`, so the FOURTH consecutive denial is the one that ends the run.
DENIALS_TO_EXHAUST = PolicyConfig().max_policy_denials + 1


@pytest.fixture(autouse=True)
def _browser_backed_provider():
    """`build()` names `BROWSER_PROVIDER`; register it for one test and leave
    the registry as found (the same fixture `test_orchestrator.py` owns)."""
    from autoloop import conversation as conversation_module

    register_provider(BROWSER_PROVIDER, lambda config: None, browser_backed=True)
    try:
        yield
    finally:
        conversation_module._PROVIDERS.pop(BROWSER_PROVIDER, None)
        conversation_module._BROWSER_BACKED.discard(BROWSER_PROVIDER)


def events(orch, entry_type: str) -> list[dict]:
    """The `data` payload of every transcript entry of one type."""
    path = orch._config.transcript_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == entry_type:
            out.append(entry.get("data") or {})
    return out


def advance_to_executing(orch) -> None:
    """Step the real phase machine up to `executing`, the phase whose step
    parses the directive and dispatches it. Bounded so a loop that never gets
    there fails as an assertion rather than by hanging."""
    for _ in range(10):
        phase = Phase(orch.state.phase)
        if phase is Phase.EXECUTING:
            return
        orch._step(phase)
    raise AssertionError(f"never reached executing (phase={orch.state.phase})")


def bare_pushes(count: int):
    """`count` replies that each stamp the packet they answer and approve a
    `push` bound to nothing — the reviewer's exact behaviour in the incident."""
    return [approval(decision="push") for _ in range(count)]


# ---- the claim --------------------------------------------------------------


def test_four_consecutive_bare_pushes_stop_the_run(tmp_path):
    """THE regression. Four denials of the same refused directive end the run
    on `policy_denial_budget_exhausted`, driven end to end through `run()`.

    Before the fix this returned no terminal at all: the loop denied, re-
    prompted, denied again and only stopped when `FakeClient` ran out of
    scripted replies — the fixture's version of the 14-minute livelock.

    The branch is `build()`'s default `feature/x` and NOT a protected one, which
    is what makes this the incident's path rather than
    `test_orchestrator.py::test_denied_push_is_reported_not_executed`'s: a push
    to `main` is refused by `authorize_directive` above the dispatch, and that
    denial always counted."""
    orch, store, git, _, _, _, _ = build(
        tmp_path, responses=bare_pushes(DENIALS_TO_EXHAUST + 2)
    )

    assert orch.run() == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"

    stopped = events(orch, "stopped")
    assert [entry["code"] for entry in stopped] == ["policy_denial_budget_exhausted"]
    assert "policy-denied directives in a row" in orch.state.stop_reason
    assert LEGACY_REASON in orch.state.stop_reason, "the last denial is named"

    # The counter reached the budget instead of oscillating, and DURABLY: the
    # in-memory value alone would have been true throughout the incident too.
    assert orch.state.policy_denials == DENIALS_TO_EXHAUST
    assert store.load().policy_denials == DENIALS_TO_EXHAUST

    denied = events(orch, "policy_denied")
    assert [entry["code"] for entry in denied] == [LEGACY] * DENIALS_TO_EXHAUST
    assert git.pushes == 0, "no bare push was ever published"


def test_the_counter_survives_every_denial_round(tmp_path):
    """The other half of the claim, watched round by round on DISK rather than
    only at the end: the persisted counter climbs 1, 2, 3, 4 across four real
    rounds. Before the fix it read 1 after every one of them."""
    orch, store, _, _, _, _, _ = build(
        tmp_path, responses=bare_pushes(DENIALS_TO_EXHAUST)
    )

    observed = []
    for _ in range(DENIALS_TO_EXHAUST):
        advance_to_executing(orch)
        orch._step_executing()
        observed.append(store.load().policy_denials)

    assert observed == [1, 2, 3, 4]
    assert orch.state.phase == Phase.STOPPED.value
    # Rounds 1-3 re-prompted rather than parking, so the budget bounded a loop
    # that was genuinely still running.
    assert len(events(orch, "policy_denied")) == DENIALS_TO_EXHAUST


def test_an_acted_on_directive_still_clears_the_streak(tmp_path):
    """The counter must stay CONSECUTIVE, not become a run-level total — a fix
    that simply stopped clearing would end healthy runs whose denials were
    spread over unrelated rounds. Two bare pushes, then a directive the loop
    acts on: the streak is over, and the clear is persisted."""
    orch, store, _, _, _, _, _ = build(
        tmp_path, responses=[*bare_pushes(2), stop_block()]
    )

    assert orch.run() == Phase.STOPPED.value
    assert orch.state.stop_kind != "fault", "the reviewer's own stop, not the budget"
    assert len(events(orch, "policy_denied")) == 2
    assert orch.state.policy_denials == 0
    assert store.load().policy_denials == 0, "and durably, so a restart agrees"


def test_a_denial_at_the_gate_and_one_inside_the_dispatch_are_consecutive(tmp_path):
    """The two sides of the clear spend ONE budget between them. A reviewer
    alternating a refused-at-the-gate directive with a refused-inside-the-
    dispatch one must not be able to keep either counter from ever binding —
    which is the livelock rebuilt out of two halves.

    `commit` is the gate denial here (`policy.allow_commit=false`), a bare
    `push` the dispatch one."""
    orch, store, _, _, _, _, _ = build(
        tmp_path,
        responses=[
            approval(decision="commit"),
            approval(decision="push"),
            approval(decision="commit"),
            approval(decision="push"),
            approval(decision="commit"),
        ],
        policy=PolicyConfig(allow_commit=False),
    )

    assert orch.run() == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    assert store.load().policy_denials == DENIALS_TO_EXHAUST

    codes = [entry["code"] for entry in events(orch, "policy_denied")]
    assert codes == ["commit_disabled", LEGACY, "commit_disabled", LEGACY]


def test_the_budget_binds_with_autonomy_enabled(tmp_path):
    """The incident ran in autonomous mode, where `_to_fault_stop` hands
    `policy_denial_budget_exhausted` to `_autonomous_fault_set_aside` and the
    run ends by setting the ONE task in flight aside instead (halt-01). That
    branch is a different terminal, so it needs its own evidence: the budget
    still binds, the counter still reaches four, and the loop still ends.

    Without this the fix would be pinned only on the path production does not
    take — the fail-open shape where the alarm is proven in a configuration
    nobody runs."""
    orch, store, _, _, _, _, _ = build(
        tmp_path, responses=bare_pushes(DENIALS_TO_EXHAUST + 2)
    )
    config = orch._config
    object.__setattr__(config, "autonomy", AutonomyConfig(enabled=True))
    orch._blocker_store = BlockerStore(config.blockers_dir)
    # The round in flight, which is what a set-aside quarantines. A bare `push`
    # names no task, so `_autonomous_set_aside_task` falls back to this record.
    orch.state.task_execution = {"task_id": "t1"}

    assert orch.run() == Phase.NEEDS_USER.value, "the set-aside park, not a stop"
    assert store.load().policy_denials == DENIALS_TO_EXHAUST

    set_aside = events(orch, "autonomous_fault_set_aside")
    assert [entry["code"] for entry in set_aside] == [
        "policy_denial_budget_exhausted"
    ]
    assert set_aside[0]["task_id"] == "t1"
    assert len(events(orch, "policy_denied")) == DENIALS_TO_EXHAUST
