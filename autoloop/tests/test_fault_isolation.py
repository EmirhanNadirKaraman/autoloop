"""Fault isolation: `lane_fatal` versus `fleet_fatal` — conc-07.

Candidate 6 of the nine in docs/AUTOLOOP.md, "Running several tasks at once —
the split plan", Decision 5. One claim, in two halves:

    A lane-fatal condition stops ONE lane and no other; an unrecognised kind
    still stops EVERYTHING.

The split rides a SEPARATE AXIS from `kind`, and section 1 is what pins that:
no park site's `kind` changed, `state.park_kind` still carries literally
`"loop_fatal"`, and the new question — how far does this park reach — is
answered from the CODE by `blockers.fatal_scope`. That is why the ~80 existing
assertions about `park_kind == "loop_fatal"` are untouched by this candidate,
and it is the acceptance criterion every candidate carries.

Six sections:

1. **The table is complete and explicit.** Every `loop_fatal` call site in
   `orchestrator.py` is AST-walked and its code must appear in
   `LANE_FATAL_CODES` or `FLEET_FATAL_CODES` — being in neither is a failing
   test, not a silent fleet-fatal default. The walk also pins the ONE site whose
   `code=` is not a literal, so a future site that hides its code behind a
   variable fails loudly instead of vanishing from the check.
2. **The default direction.** Absent, empty, unknown, non-string and hard-halt
   codes are all `FLEET_FATAL`, and `checkout_escape_detected` is fleet-fatal
   through a second lock that a table edit alone cannot undo.
3. **A blocker record names the lane.**
4. **`fleet_stop` reads a live park, not history**, and fails closed on every
   absence of evidence: an unreadable lane state, an unlistable lanes
   directory, a park with no kind, an unknown kind, an unknown code, a missing
   blocker id, a record that will not decode.
5. **The loop.** `cli._run_continuous` at `lanes = 2`: a sibling's lane-fatal
   park leaves this lane advancing, and a sibling's fleet-fatal park stops it
   before anything is selected.
6. **And at `lanes = 1` none of it is reached** — the same seeded sibling park
   is invisible, which is the acceptance criterion made structural.

No git repository, no subprocess and no agent: every claim here is about a
table, a small JSON file and a predicate. Self-contained per this codebase's
convention (see `test_blockers.py`'s docstring) — the small config/orchestrator
helpers are duplicated here rather than imported from another test module.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.blockers import (
    FLEET_FATAL,
    FLEET_FATAL_CODES,
    HARD_HALT_CODES,
    LANE_FATAL,
    LANE_FATAL_CODES,
    NO_TASK,
    BlockerStore,
    fatal_scope,
)
from autoloop.config import (
    AutoloopConfig,
    BrowserConfig,
    ConcurrencyConfig,
    lane_id,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    UNLISTABLE_LANES_INDEX,
    FleetStop,
    Orchestrator,
    fleet_stop,
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

URL = "https://chatgpt.com/c/fault-isolation-test"

#: The ONE blocker-emitting site in `orchestrator.py` whose `code=` is not a
#: string literal: `_handle_quota_exhausted`'s local `park()` helper, which takes
#: the code as a parameter and is called with `quota_exhausted` and
#: `provider_switch_budget`.
#:
#: Named as a dotted path (outer function, then the nested one) so the allowlist
#: identifies a place rather than a common word, and asserted as an EQUALITY in
#: section 1: skipping non-literal sites quietly would be the check switching
#: itself off — a future park site whose code is a variable would be
#: unclassified, invisible, and passing.
OPAQUE_CODE_SITES = {"_handle_quota_exhausted.park"}

#: The two codes that site emits. Listed here because the AST walk cannot read
#: them out of the call it makes, so their classification is pinned by name.
OPAQUE_SITE_CODES = ("quota_exhausted", "provider_switch_budget")


# =============================================================================
# helpers
# =============================================================================


def make_config(tmp_path: Path, lanes: int = 2) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers_root",
        concurrency=ConcurrencyConfig(lanes=lanes),
    )


def continuous_args() -> argparse.Namespace:
    return argparse.Namespace(config=None, continuous=True, null_executor=False)


class StopTheLoop(Exception):
    """Ends `_run_continuous` from inside a doubled selection — reaching it at
    all is half of every assertion in section 5."""


def a_task(task_id: str) -> Task:
    return Task(
        id=task_id,
        title=f"task {task_id}",
        description="a task this lane could pick up",
        approved_paths=("autoloop/tests/",),
    )


def park_a_lane(
    config: AutoloopConfig,
    index: int,
    *,
    code: str,
    kind: str | None = "loop_fatal",
    phase: str = Phase.NEEDS_USER.value,
    stop_kind: str = "",
    with_blocker: bool = True,
) -> str:
    """Seed lane `index` with a terminal an operator would have to answer, and
    the durable blocker record that carries its code. Returns the blocker id
    (`""` when the caller asked for no record).

    Written directly rather than produced by a round, for the reason the split
    plan gives about the merge candidate: the claim is about two lanes' RECORDS,
    which a test writes, not about two live agents.
    """
    blocker_id = ""
    if with_blocker:
        record = BlockerStore(config.blockers_dir).record(
            task_id=NO_TASK,
            kind=kind or "loop_fatal",
            code=code,
            question=f"a {code} question",
            detail="",
            phase=phase,
            now=utcnow_iso(),
            lane_id=lane_id(index),
        )
        blocker_id = record.id
    state = LoopState(session_id=f"lane-{index}", conversation_url=URL)
    state.phase = phase
    state.question = f"a {code} question"
    if phase == Phase.NEEDS_USER.value:
        state.park_kind = kind
        state.park_blocker_id = blocker_id or None
    else:
        state.stop_kind = stop_kind
        state.stop_blocker_id = blocker_id or None
    StateStore(lane_paths(config.state_dir, index).state_file).save(state)
    return blocker_id


def transcript_entries(config: AutoloopConfig, entry_type: str) -> list[dict]:
    """The `data` payload of every transcript entry of one type."""
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [row.get("data") or {} for row in rows if row.get("type") == entry_type]


def emitting_sites() -> list[tuple[str, ast.Call]]:
    """`(dotted enclosing function, call node)` for every `_to_needs_user` /
    `_to_fault_stop` call in `orchestrator.py`, attributed to the INNERMOST
    function that contains it.

    Innermost, because the one site whose code is not a literal is inside a
    nested helper and has to be nameable on its own; dotted, because `park` on
    its own would name a place nobody could find.
    """
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    found: list[tuple[str, ast.Call]] = []

    def visit(node, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, f"{where}.{child.name}" if where else child.name)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )
                if name in ("_to_needs_user", "_to_fault_stop"):
                    found.append((where, child))
            visit(child, where)

    visit(tree, "")
    return found


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def site_kind(call: ast.Call) -> str:
    """The `kind` a call site declares. A site that names none takes
    `_to_needs_user`'s own default, and a site whose kind is not a literal is
    read as `loop_fatal` — the fail-closed direction, so a computed kind is
    CLASSIFIED rather than skipped."""
    value = _kwarg(call, "kind")
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return "loop_fatal"


def site_codes(call: ast.Call) -> set[str]:
    """Every string literal reachable as this site's `code=`, so a code built
    from a conditional yields both branches. An absent `code=` is the method's
    own default; a `code=` with no literal in it at all yields nothing, which is
    what `OPAQUE_CODE_SITES` accounts for."""
    value = _kwarg(call, "code")
    if value is None:
        return {"unclassified"}
    return {
        sub.value
        for sub in ast.walk(value)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    }


# =============================================================================
# 1. every loop_fatal call site is classified explicitly
# =============================================================================


def test_every_loop_fatal_call_site_is_classified_by_the_table():
    """THE test the split plan asks this candidate for. A code neither set names
    is not "fleet-fatal by default" here — it is a park nobody reasoned about,
    and the table is the place that reasoning is written down. The runtime
    default in `fatal_scope` still covers it (section 2); this is what makes
    leaving it uncovered a failing test rather than a silent choice."""
    classified = LANE_FATAL_CODES | FLEET_FATAL_CODES
    unclassified: dict[str, set[str]] = {}
    for where, call in emitting_sites():
        if site_kind(call) != "loop_fatal":
            continue
        missing = site_codes(call) - classified
        if missing:
            unclassified.setdefault(where, set()).update(missing)
    assert not unclassified, (
        "loop_fatal park sites whose code is in neither LANE_FATAL_CODES nor "
        f"FLEET_FATAL_CODES: { {k: sorted(v) for k, v in unclassified.items()} }"
    )


def test_the_one_site_whose_code_is_not_a_literal_is_named_rather_than_skipped():
    """THE FAIL-OPEN this walk would otherwise have. A site whose `code=` is a
    variable yields no literal, so it drops out of the test above without
    failing it — the guard switching itself off for exactly the site nobody
    wrote down. Asserted as an EQUALITY against a named allowlist, so a new
    opaque site is a failing test, and the two codes that one site really emits
    are classified by name."""
    opaque = {
        where
        for where, call in emitting_sites()
        if site_kind(call) == "loop_fatal" and not site_codes(call)
    }
    assert opaque == OPAQUE_CODE_SITES
    for code in OPAQUE_SITE_CODES:
        assert code in FLEET_FATAL_CODES, f"{code} is emitted but unclassified"


def test_the_two_sets_are_disjoint_and_every_entry_is_a_code():
    """A code in both sets would make the classification depend on which lookup
    ran first. Asserted the way `test_autonomous_recovery` asserts the hard-halt
    disjointness, and for the same reason: the second lock is only a lock while
    something checks the two agree."""
    assert LANE_FATAL_CODES.isdisjoint(FLEET_FATAL_CODES)
    assert LANE_FATAL_CODES and FLEET_FATAL_CODES
    for code in LANE_FATAL_CODES | FLEET_FATAL_CODES:
        assert isinstance(code, str) and code.strip() == code and code


def test_the_document_settles_two_of_the_entries():
    """Two classifications are the DOCUMENT's, not this candidate's judgement:
    Decision 5 keeps `checkout_escape_detected` fleet-fatal deliberately, and
    Decision 8 says an unclean observed clone "parks that lane and no other"."""
    assert fatal_scope("checkout_escape_detected") == FLEET_FATAL
    assert fatal_scope("observed_checkout_unusable") == LANE_FATAL


# =============================================================================
# 2. the default direction — unrecognised means the fleet stops
# =============================================================================


@pytest.mark.parametrize(
    "code",
    ["", "unclassified", "some_future_park_nobody_reasoned_about", None, 17, object()],
)
def test_an_absent_unknown_or_unreadable_code_is_fleet_fatal(code):
    """Decision 5's own sentence — "an unrecognised or absent kind is
    fleet_fatal" — applied to the code, including the shapes a hand-edited
    record can produce: a non-string is not a code this build knows, so it
    cannot be read as one lane's business."""
    assert fatal_scope(code) == FLEET_FATAL


def test_a_hard_halt_is_refused_before_the_table_is_consulted(monkeypatch):
    """The second, redundant lock. Adding `checkout_escape_detected` to
    `LANE_FATAL_CODES` by hand must not be enough to move it — the refusal is
    ahead of the lookup, so the table edit changes nothing and the disjointness
    test above turns the edit itself into a failure."""
    monkeypatch.setattr(
        "autoloop.blockers.LANE_FATAL_CODES",
        frozenset(LANE_FATAL_CODES | HARD_HALT_CODES),
    )
    for code in HARD_HALT_CODES:
        assert fatal_scope(code) == FLEET_FATAL, f"{code} escaped the second lock"


def test_the_words_are_distinct():
    assert LANE_FATAL != FLEET_FATAL
    assert (LANE_FATAL, FLEET_FATAL) == ("lane_fatal", "fleet_fatal")


# =============================================================================
# 3. a blocker record names the lane
# =============================================================================


def build_orchestrator(tmp_path, lane_index: int, lanes: int = 2):
    """A collaborator-free Orchestrator in one lane — `_to_needs_user` touches
    only `state`, `_log`, `_blocker_store` and `_store`."""
    config = make_config(tmp_path, lanes=lanes)
    store = StateStore(lane_paths(config.state_dir, lane_index).state_file)
    state = LoopState.new(URL)
    store.save(state)
    registry = TaskRegistry([a_task("t1")])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    blocker_store = BlockerStore(config.blockers_dir)
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
        lane_index=lane_index,
    )
    return orch, config, blocker_store


def test_a_park_records_the_lane_that_raised_it(tmp_path):
    """Decision 5's "its blocker record names the lane", and the reason it is a
    field: an operator reading `blockers` after a fleet-fatal stop is looking at
    records from several lanes, and nothing else on the record says which."""
    orch, _config, blockers = build_orchestrator(tmp_path, lane_index=2)

    orch._to_needs_user(
        "the clone is dirty", kind="loop_fatal", code="observed_checkout_unusable"
    )

    open_records = blockers.open_blockers()
    assert [b.lane_id for b in open_records] == [lane_id(2)] == ["_lane-2"]
    # And the axis the whole candidate rides on is untouched.
    assert orch.state.park_kind == "loop_fatal"


def test_a_fault_stop_records_the_lane_too(tmp_path):
    """A fault stop is not a park, but it is a `loop_fatal` record read out of
    the same list — so it carries the same attribution."""
    orch, _config, blockers = build_orchestrator(tmp_path, lane_index=1)

    orch._to_fault_stop(
        "the reviewer spent the denial budget",
        code="policy_denial_budget_exhausted",
    )

    assert [b.lane_id for b in blockers.open_blockers()] == [lane_id(1)]


def test_a_bump_never_blanks_the_lane_a_record_already_names(tmp_path):
    """`record` upserts on (task, code, phase). A caller that supplies no lane
    is an ABSENCE of information, and overwriting a named lane with it would
    lose the only field that says where the fault was raised; a caller that
    supplies one wins, so the record names where it was LAST seen."""
    blockers = BlockerStore(tmp_path / "blockers")
    common = dict(
        task_id="t1", kind="loop_fatal", code="rate_limited",
        question="q", detail="d", phase="ready",
    )
    first = blockers.record(now=utcnow_iso(), lane_id="_lane-3", **common)
    kept = blockers.record(now=utcnow_iso(), **common)
    moved = blockers.record(now=utcnow_iso(), lane_id="_lane-0", **common)

    assert (first.id, first.lane_id) == (kept.id, "_lane-3")
    assert kept.lane_id == "_lane-3", "a bump with no lane blanked the record"
    assert moved.lane_id == "_lane-0"


def test_a_record_written_before_the_field_existed_reads_as_no_lane(tmp_path):
    """Same tolerance every other added field has: absent is `""`, and `""`
    classifies exactly as a named lane does — nothing branches on it."""
    directory = tmp_path / "blockers"
    directory.mkdir()
    old = {
        "id": "blk-(loop)-001", "task_id": NO_TASK, "kind": "loop_fatal",
        "code": "rate_limited", "question": "q", "detail": "", "phase": "ready",
        "created_at": utcnow_iso(),
    }
    (directory / "blk-(loop)-001.json").write_text(json.dumps(old), encoding="utf-8")

    loaded = BlockerStore(directory).load("blk-(loop)-001")

    assert loaded is not None and loaded.lane_id == ""
    assert fatal_scope(loaded.code) == FLEET_FATAL


# =============================================================================
# 4. `fleet_stop` — what one lane learns about the others
# =============================================================================


def test_a_lane_fatal_park_is_not_a_fleet_stop(tmp_path):
    """THE first half of the claim, at the predicate. Lane 1 parked on an
    unclean observed clone; lane 0 has no reason to stop."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(config, 1, code="observed_checkout_unusable")

    assert fleet_stop(config, exclude=0) is None


def test_a_fleet_fatal_park_stops_the_others_and_names_the_lane(tmp_path):
    """The second half. The answer carries the lane, the code and the durable
    record, so the lane that stops can say where to look."""
    config = make_config(tmp_path, lanes=2)
    blocker_id = park_a_lane(config, 1, code="worker_environment_drift")

    stop = fleet_stop(config, exclude=0)

    assert stop is not None and stop.readable
    assert (stop.lane_index, stop.lane_id) == (1, lane_id(1))
    assert (stop.code, stop.kind, stop.blocker_id) == (
        "worker_environment_drift", "loop_fatal", blocker_id
    )
    assert lane_id(1) in stop.describe() and "worker_environment_drift" in stop.describe()


def test_an_escape_stops_the_fleet(tmp_path):
    """Decision 5's one named carve-out, end to end: the boundary is per-lane
    and the violation is attributable, and an escape is fleet-fatal anyway — an
    agent that has demonstrated it writes outside its boundary is not one to
    keep three neighbours running alongside."""
    config = make_config(tmp_path, lanes=4)
    park_a_lane(config, 2, code="checkout_escape_detected")

    stop = fleet_stop(config, exclude=0)

    assert stop is not None and stop.code == "checkout_escape_detected"
    assert stop.lane_index == 2


@pytest.mark.parametrize(
    "kind", [None, "", "task-fatal", "lane_fatal", "LOOP_FATAL", "whatever"]
)
def test_a_park_with_no_kind_or_an_unknown_kind_stops_the_fleet(tmp_path, kind):
    """"An unrecognised or absent kind is fleet_fatal" — the direction Decision 5
    keeps, applied to a state file written by an older build, hand-edited, or
    written by a build that knows a word this one does not. The code is
    deliberately a LANE-fatal one, so what is measured is the kind and not the
    code falling through the same default."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(config, 1, code="observed_checkout_unusable", kind=kind)

    stop = fleet_stop(config, exclude=0)

    assert stop is not None and stop.lane_index == 1


def test_a_task_fatal_park_stops_nothing(tmp_path):
    """Narrower than either word: one task is set aside and that lane goes on
    working, which is what `cli._handle_parked_task` already does with it."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(config, 1, code="commit_refused", kind="task_fatal")

    assert fleet_stop(config, exclude=0) is None


@pytest.mark.parametrize(
    "phase, stop_kind",
    [
        (Phase.READY.value, ""),
        (Phase.EXECUTING.value, ""),
        (Phase.STOPPED.value, ""),
        (Phase.STOPPED.value, "contract"),
        (Phase.STOPPED.value, "preempted"),
    ],
)
def test_a_lane_that_has_not_parked_is_not_a_stop(tmp_path, phase, stop_kind):
    """A lane mid-round is not a stop and must not be one — "every lane is
    stopped at its next safe phase" is a boundary, never an interruption of a
    round in flight. A contract stop and a preemption are the clean boundaries
    they have always been."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(
        config, 1, code="checkout_escape_detected", phase=phase, stop_kind=stop_kind
    )

    assert fleet_stop(config, exclude=0) is None


def test_a_fault_stop_in_another_lane_stops_the_fleet(tmp_path):
    """`_to_fault_stop` ends the run rather than parking, and records the same
    `loop_fatal` blocker. It is a terminal a lane reached, so a sibling reads it
    exactly as it reads a park."""
    config = make_config(tmp_path, lanes=2)
    blocker_id = park_a_lane(
        config,
        1,
        code="policy_denial_budget_exhausted",
        phase=Phase.STOPPED.value,
        stop_kind="fault",
    )

    stop = fleet_stop(config, exclude=0)

    assert stop is not None and stop.blocker_id == blocker_id
    assert stop.code == "policy_denial_budget_exhausted"


def test_a_lane_that_names_no_blocker_stops_the_fleet(tmp_path):
    """A park whose code cannot be established is a park whose reach cannot be
    established. Fail-closed: no record id on the state file is an absence of
    evidence, not evidence that one lane's business is all this is."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(config, 1, code="observed_checkout_unusable", with_blocker=False)

    stop = fleet_stop(config, exclude=0)

    assert stop is not None and stop.code == ""


def test_a_blocker_id_naming_nothing_stops_the_fleet(tmp_path):
    """The record is gone (archived by hand, a directory wiped): same absence,
    same answer."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(config, 1, code="observed_checkout_unusable")
    for path in config.blockers_dir.glob("blk-*.json"):
        path.unlink()

    assert fleet_stop(config, exclude=0) is not None


def test_a_blocker_record_that_will_not_decode_stops_the_fleet(tmp_path):
    """`BlockerStore.load` RAISES on a corrupt record rather than reading as
    absent — and a caller deciding whether a fleet keeps running must not turn
    that raise into "carry on". It is caught and answered as a stop."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(config, 1, code="observed_checkout_unusable")
    for path in config.blockers_dir.glob("blk-*.json"):
        path.write_text("{not json", encoding="utf-8")

    assert fleet_stop(config, exclude=0) is not None


def test_a_lane_state_that_cannot_be_read_stops_the_fleet(tmp_path):
    """The lane that is in trouble is exactly the lane whose park nobody can
    read. `lane_occupants` answers "busy" for this, which holds admission; this
    answers "stop", because sizing a fleet and deciding whether a fleet that may
    be compromised keeps running are different questions."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(config, 1, code="observed_checkout_unusable")
    lane_paths(config.state_dir, 1).state_file.write_text("{ nope", encoding="utf-8")

    stop = fleet_stop(config, exclude=0)

    assert stop is not None and not stop.readable
    assert stop.lane_index == 1 and stop.code == ""
    assert "could not be read" in stop.describe()


def test_a_lanes_directory_that_cannot_be_listed_stops_the_fleet(tmp_path, monkeypatch):
    """The same absence one level up: a scan that reports nothing when it can
    see nothing would let the fleet run past a park it never looked for.
    `retired_lane_occupants` fails closed on this too, into its own vocabulary
    (one unreadable occupant); this one fails closed into a stop."""
    config = make_config(tmp_path, lanes=2)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / LANES_DIRNAME).mkdir()

    def refuse(self):
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "iterdir", refuse)

    stop = fleet_stop(config, exclude=0)

    assert stop is not None and not stop.readable
    assert stop.lane_index == UNLISTABLE_LANES_INDEX


def test_a_lane_the_lowered_cap_cut_out_is_still_read(tmp_path):
    """The mirror of `retired_lane_occupants`. Lowering `lanes` from 4 to 2 does
    not end the session in lane 3, and a fleet-fatal park there is one no walk
    over `range(lanes)` would ever see."""
    config = make_config(tmp_path, lanes=4)
    park_a_lane(config, 3, code="primary_checkout_dirty")
    cut = make_config(tmp_path, lanes=2)

    stop = fleet_stop(cut, exclude=0)

    assert stop is not None and stop.lane_index == 3


def test_a_lane_does_not_stop_for_its_own_park(tmp_path):
    """Its own park is reported by `_handle_parked_task` with today's wording
    and today's exit code. Describing it a second time in the fleet's vocabulary
    would tell an operator about one park as though it were two."""
    config = make_config(tmp_path, lanes=2)
    park_a_lane(config, 0, code="checkout_escape_detected")

    assert fleet_stop(config, exclude=0) is None
    assert fleet_stop(config, exclude=1) is not None, "the exclusion is not blanket"


def test_the_walk_is_deterministic_and_reads_no_lease_or_lock(tmp_path):
    """Two fleet-fatal parks answer the lower lane index every time — the same
    "one implementation, one order" rule `blockers.primary_sort_key` states. And
    nothing is written: a scan that mutated the state directory would be a lane
    reporting on its neighbours by touching them."""
    config = make_config(tmp_path, lanes=4)
    park_a_lane(config, 3, code="primary_checkout_dirty")
    park_a_lane(config, 1, code="login_expired")
    before = {
        str(p.relative_to(config.state_dir)): p.read_bytes()
        for p in sorted(config.state_dir.rglob("*")) if p.is_file()
    }

    for _ in range(3):
        stop = fleet_stop(config, exclude=0)
        assert stop is not None and stop.lane_index == 1

    after = {
        str(p.relative_to(config.state_dir)): p.read_bytes()
        for p in sorted(config.state_dir.rglob("*")) if p.is_file()
    }
    assert after == before, "the scan wrote something"


def test_the_record_alone_is_not_the_signal(tmp_path):
    """The failure a "stop while a fleet-fatal blocker is open" rule would have,
    pinned so it is not reintroduced: `_to_needs_user` writes its record BEFORE
    it decides whether to park, so a fault autonomous mode recovered from leaves
    an OPEN `loop_fatal` record and no park at all. The lanes' own state files
    are the signal, which is what makes this a question about a live park."""
    config = make_config(tmp_path, lanes=2)
    BlockerStore(config.blockers_dir).record(
        task_id=NO_TASK, kind="loop_fatal", code="checkout_escape_detected",
        question="an escape nobody is parked on", detail="", phase="ready",
        now=utcnow_iso(), lane_id=lane_id(1),
    )

    assert fleet_stop(config, exclude=0) is None


def test_a_fleet_with_nothing_in_it_is_not_a_stop(tmp_path):
    """The ordinary state: no lane has a session at all."""
    config = make_config(tmp_path, lanes=3)

    assert fleet_stop(config, exclude=0) is None


# =============================================================================
# 5. the loop — one lane advances, the other fleet stops
# =============================================================================


def test_a_lane_fatal_park_leaves_the_other_lanes_advancing(tmp_path, monkeypatch):
    """THE claim, driven through the real `cli._run_continuous`: lane 1 is
    parked lane-fatally, and lane 0 reaches its ordinary selection on the very
    next tick."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry([a_task("t1")]))
    park_a_lane(config, 1, code="observed_checkout_unusable")
    selected: list[str] = []

    def select(cfg, store, registry):
        selected.append(registry.next_ready().id)
        raise StopTheLoop()

    monkeypatch.setattr(cli, "_select_and_kickoff", select)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)

    assert selected == ["t1"], "a lane-fatal park stopped a lane it does not own"


def test_a_fleet_fatal_park_stops_this_lane_before_anything_is_selected(
    tmp_path, monkeypatch, capsys
):
    """The other half, at the same site. Nothing is selected, nothing is
    dispatched, and the exit code is the 2 every loop-fatal terminal uses."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry([a_task("t1")]))
    park_a_lane(config, 1, code="checkout_escape_detected")
    monkeypatch.setattr(
        cli,
        "_select_and_kickoff",
        lambda *a, **k: pytest.fail("a fleet-fatal stop admitted work"),
    )

    assert cli._run_continuous(continuous_args(), config) == 2

    printed = capsys.readouterr().out
    assert lane_id(1) in printed and "checkout_escape_detected" in printed
    # And it is not only on a terminal nobody is watching: the durable evidence
    # (`blockers/`) belongs to the lane that PARKED, so this lane's own record
    # that it stopped, and for whom, is the transcript entry.
    logged = transcript_entries(config, "fleet_stop")
    assert len(logged) == 1
    assert (logged[0]["lane_id"], logged[0]["code"]) == (
        lane_id(1), "checkout_escape_detected"
    )
    assert logged[0]["readable"] is True and logged[0]["blocker_id"]


def test_an_unreadable_sibling_stops_this_lane_too(tmp_path, monkeypatch):
    """The fail-closed case through the loop rather than only at the predicate:
    a lane whose state cannot be read stops this one rather than being stepped
    over on the way to a fresh round."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry([a_task("t1")]))
    lane_paths(config.state_dir, 1).state_file.parent.mkdir(parents=True, exist_ok=True)
    lane_paths(config.state_dir, 1).state_file.write_text("{ nope", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_select_and_kickoff",
        lambda *a, **k: pytest.fail("an unreadable lane read as healthy"),
    )

    assert cli._run_continuous(continuous_args(), config) == 2


def test_an_operator_pause_still_answers_first(tmp_path, monkeypatch):
    """Order matters where two terminals are both true. `pause` and `abort` are
    an operator's own verbs and still exit 0 — a fleet stop is a fault, and
    reporting it over a stop the operator asked for would tell them their own
    request failed."""
    config = make_config(tmp_path, lanes=2)
    TaskStore(config.tasks_file).save(TaskRegistry([a_task("t1")]))
    park_a_lane(config, 1, code="checkout_escape_detected")
    monkeypatch.setattr(cli, "pause_requested", lambda cfg: True)

    assert cli._run_continuous(continuous_args(), config) == 0


# =============================================================================
# 6. and at `lanes = 1` none of it exists
# =============================================================================


def test_at_one_lane_a_sibling_park_is_never_read(tmp_path, monkeypatch):
    """THE acceptance criterion, made structural rather than asserted: at
    `lanes = 1` `_fleet_stop_reached` answers `None` without reading anything,
    so a `lanes/` directory left behind by an experiment cannot stop a loop that
    has since been turned back down to one."""
    config = make_config(tmp_path, lanes=1)
    TaskStore(config.tasks_file).save(TaskRegistry([a_task("t1")]))
    park_a_lane(config, 1, code="checkout_escape_detected")
    monkeypatch.setattr(
        cli,
        "fleet_stop",
        lambda *a, **k: pytest.fail("one lane consulted the fleet"),
    )
    selected: list[str] = []

    def select(cfg, store, registry):
        selected.append(registry.next_ready().id)
        raise StopTheLoop()

    monkeypatch.setattr(cli, "_select_and_kickoff", select)

    with pytest.raises(StopTheLoop):
        cli._run_continuous(continuous_args(), config)

    assert selected == ["t1"]


def test_the_gate_is_the_only_thing_between_one_lane_and_the_scan(tmp_path):
    """The helper itself, at both settings, so the gate is a value rather than a
    behaviour inferred from a loop that did not stop."""
    one = make_config(tmp_path, lanes=1)
    park_a_lane(one, 1, code="checkout_escape_detected")

    assert cli._fleet_stop_reached(one, BlockerStore(one.blockers_dir), None) is None

    two = make_config(tmp_path, lanes=2)
    reached = cli._fleet_stop_reached(two, BlockerStore(two.blockers_dir), None)
    assert isinstance(reached, FleetStop) and reached.lane_index == 1
