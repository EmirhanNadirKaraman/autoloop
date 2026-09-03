"""conc-04 — one observed checkout per lane, and what that buys.

THE CLAIM THESE TESTS EXIST TO PIN, in the three parts it was stated in
(docs/AUTOLOOP.md, "Decision 1 — the isolation boundary is one observed
checkout per lane"):

  1. each lane's escape detector brackets a tree only that lane writes — so a
     SIBLING lane synchronising mid-window is not a violation in lane A;
  2. a genuine escape inside a lane is still detected, with its path;
  3. a write by one lane is not attributed to another — the provenance half,
     which is `.git/FETCH_HEAD` inside a worker repo naming ITS OWN lane's
     clone and no other's.

AND THE ACCEPTANCE CRITERION EVERY CANDIDATE IN THE SPLIT CARRIES: at
`lanes = 1` nothing moves. Lane 0's clone is exactly `[paths].observed_checkout`
as `load_config` resolves it today, and `ObservedCheckout.for_lane` hands back
the very object it was called on.

PART 1 IS PAIRED WITH ITS CONTROL, deliberately. "No violation in lane A" is
nearly tautological once A and B are separate directories — `ls-files` in one
cannot see the other — so the isolated case alone would be a restatement of the
layout rather than evidence about it. The shared-clone control right beside it
reproduces the failure Decision 1 exists to fix (a `synchronize` rewrites the
whole working tree, and lane A's window then reports every path in the
repository), so the pair says what isolation actually removed.

Self-contained per this codebase's convention (see `test_observed_checkout.py`,
whose claims these extend one lane over) — real git repositories where the
claim is about git, and nothing imported from another test module except the
repository builder in `gitrepo.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gitrepo import make_repo_from_template, run_git

from autoloop import escape_detector
from autoloop.config import (
    AutoloopConfig,
    BrowserConfig,
    ConcurrencyConfig,
    MAX_LANES,
    default_observed_checkout,
    lane_id,
    lane_observed_checkout,
    load_config,
)
from autoloop.contract import Decision, Directive
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore, mutation_ledger_for
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import (
    ObservedCheckout,
    WorkerRepoManager,
    validate_lane_checkout_distinctness,
    validate_observed_checkout,
)
from autoloop.worktask import IntentStore, TaskExecutionStore

URL = "https://chatgpt.com/c/conc-04-one-checkout-per-lane"


def a_config(tmp_path: Path, lanes: int = 1, observed: Path | None = None):
    """The cheapest real config for these claims: a state dir, a workers root
    and a fleet size. Same shape as `test_lane_state.make_config`."""
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers_root",
        observed_checkout=observed,
        concurrency=ConcurrencyConfig(lanes=lanes),
    )


def head_of(repo_root: Path) -> str:
    return run_git(repo_root, "rev-parse", "HEAD").strip()


def commit_readme(repo_root: Path, body: str) -> str:
    (repo_root / "README.md").write_text(body, encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "move the head")
    return head_of(repo_root)


def open_window(clone: ObservedCheckout):
    """The "before" half of `orchestrator._execute_with_escape_detection`,
    against one lane's clone."""
    git = clone.gateway(PolicyEngine(PolicyConfig()))
    paths = escape_detector.enumerate_checkout_paths(git)
    return git, paths, escape_detector.snapshot_checkout(git.repo_root, paths)


def close_window(git, paths_before, before) -> list[str]:
    """The "after" half, re-enumerating exactly as the orchestrator does — a
    path created inside the window is not in `paths_before` at all."""
    paths_after = escape_detector.enumerate_checkout_paths(git)
    after = escape_detector.snapshot_checkout(
        git.repo_root, sorted(set(paths_before) | set(paths_after))
    )
    return escape_detector.diff_snapshots(before, after)


# =============================================================================
# 1. The path rule — one lane is today, and above one lane nobody nests
# =============================================================================


def test_at_one_lane_lane_zero_is_exactly_the_configured_observed_checkout(tmp_path):
    """The acceptance criterion, asked of the two ways a deployment gets an
    observed checkout: the default beside `workers_root`, and an explicit
    `[paths].observed_checkout`. Both are read through `load_config`, so this
    fails if the resolution moves rather than only if this function does."""
    cfg = tmp_path / "config.toml"
    workers = tmp_path / "work" / "workers"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nworkers_root = "{workers}"\n',
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.observed_checkout == default_observed_checkout(workers)
    assert lane_observed_checkout(config.observed_checkout, 0, 1) == config.observed_checkout

    elsewhere = tmp_path / "elsewhere"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nworkers_root = "{workers}"\n'
        f'observed_checkout = "{elsewhere}"\n',
        encoding="utf-8",
    )
    explicit = load_config(cfg)
    assert explicit.concurrency.lanes == 1, "the shipped fleet size is still one"
    assert lane_observed_checkout(explicit.observed_checkout, 0, 1) == elsewhere


@pytest.mark.parametrize("lanes", range(2, MAX_LANES + 1))
def test_above_one_lane_no_two_lanes_share_or_contain_a_tree(tmp_path, lanes):
    """Distinctness is a property of the DERIVATION, not of the validator: for
    every fleet size this build will accept, every pair of lanes is a different
    directory and neither is inside the other. Asserted through
    `validate_observed_checkout` as well, so the derivation and the rule that
    refuses a bad layout are pinned against each other rather than separately."""
    root = tmp_path / "observed"
    paths = [lane_observed_checkout(root, index, lanes) for index in range(lanes)]

    assert len(set(paths)) == lanes, paths
    assert paths[0] != root, "above one lane even lane 0 nests"
    for index, path in enumerate(paths):
        assert path == root / lane_id(index)
        others = [other for other in paths if other != path]
        assert validate_observed_checkout(
            path, tmp_path / "repo", tmp_path / ".al", tmp_path / "workers_root", others
        ) == []


def test_a_lane_outside_the_cap_never_collapses_onto_lane_zeros_tree(tmp_path):
    """The fail-open this rule is shaped to avoid. An operator who lowers
    `[concurrency] lanes` does not end the sessions in the lanes the new cap
    cut out (`orchestrator.retired_lane_occupants`), so a lane whose index is
    at or above the fleet size is exactly the lane that can still be RUNNING —
    and resolving it to the bare configured path would put it on the same clone
    as lane 0, which is two lanes in one tree."""
    root = tmp_path / "observed"

    assert lane_observed_checkout(root, 3, 2) == root / lane_id(3)
    assert lane_observed_checkout(root, 1, 1) == root / lane_id(1)
    assert lane_observed_checkout(root, 0, 1) == root
    for index in range(1, MAX_LANES + 2):
        for lanes in range(1, MAX_LANES + 1):
            assert lane_observed_checkout(root, index, lanes) != root


def test_a_deployment_that_watches_nothing_still_watches_nothing(tmp_path):
    """`None` in, `None` out — the pre-esc-02 deployment that observes the
    primary checkout. It is reachable only from a hand-built config (`load_
    config` always resolves one), and inventing a path for it here would wire a
    tree nothing asked for."""
    assert lane_observed_checkout(None, 0, 1) is None
    assert lane_observed_checkout(None, 2, 4) is None


@pytest.mark.parametrize("index", [False, True, -1, 1.0, "1"])
def test_an_index_that_is_not_a_lane_is_refused(tmp_path, index):
    """`lane_id`'s refusal, reached before anything branches on lane 0:
    `_lane-True` would be a second spelling of lane 1's directory."""
    with pytest.raises(ValueError):
        lane_observed_checkout(tmp_path / "observed", index, 2)


@pytest.mark.parametrize("lanes", [0, -1, True, 2.0, "2", None])
def test_a_fleet_size_that_is_not_a_fleet_is_refused(tmp_path, lanes):
    """Held to the same standard as the index, and for the sharper reason: "one
    lane" is the single reading under which lane 0 keeps the shared path, so a
    value that cannot be read must not be guessed at as one."""
    with pytest.raises(ValueError):
        lane_observed_checkout(tmp_path / "observed", 0, lanes)


def test_for_lane_keeps_the_object_at_one_lane_and_the_runner_above_it(tmp_path):
    """A derived clone that dropped `runner` would hand a test's injected git
    failures back to the real `subprocess.run` — a guard switching itself off
    exactly where a test believed it had armed one."""
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        raise AssertionError("this runner is never meant to run a command")

    clone = ObservedCheckout(tmp_path / "observed", runner=runner)

    assert clone.for_lane(0, 1) is clone
    assert clone.for_lane(0) is clone

    lane_one = clone.for_lane(1, 2)
    assert lane_one is not clone
    assert lane_one.path == (tmp_path / "observed" / lane_id(1)).resolve()
    assert lane_one._runner is runner
    assert calls == []


# =============================================================================
# 2. The rule that refuses a lane set assembled some other way
# =============================================================================


def test_two_lanes_pointed_at_one_tree_are_refused(tmp_path):
    shared = tmp_path / "observed" / lane_id(0)

    violations = validate_observed_checkout(
        shared, tmp_path / "repo", tmp_path / ".al", tmp_path / "workers_root", [shared]
    )

    assert violations, "two lanes on one clone is the arrangement this removes"
    assert "IS another lane's observed checkout" in violations[0]
    # `.resolve()` on both sides of every path comparison in this file: the
    # validator reports resolved paths, and a macOS `tmp_path` reaches its
    # temporary directory through a symlink.
    assert str(shared.resolve()) in violations[0]


def test_a_lane_nested_in_another_is_refused_from_both_sides(tmp_path):
    """The same `_is_nested` test in both directions the state dir and
    `workers_root` already get, extended to siblings — and BOTH directions are
    asserted, because a rule that only looked downwards would accept the
    arrangement `state.lane_paths`' index-only asymmetry would have produced
    (lane 0 at the root, lane 1 inside it)."""
    outer = tmp_path / "observed"
    inner = outer / lane_id(1)
    repo, state, workers = tmp_path / "repo", tmp_path / ".al", tmp_path / "workers_root"

    inner_first = validate_observed_checkout(inner, repo, state, workers, [outer])
    assert inner_first, "a lane inside another lane's clone is a violation"
    assert "is nested beneath another lane's observed checkout" in inner_first[0]
    assert str(outer.resolve()) in inner_first[0]

    outer_first = validate_observed_checkout(outer, repo, state, workers, [inner])
    assert outer_first, "and so is a lane whose clone contains another's"
    assert "is nested beneath observed_checkout" in outer_first[0]
    assert str(inner.resolve()) in outer_first[0]


def test_a_sibling_path_that_is_not_absolute_is_refused_not_resolved(tmp_path):
    """The same argument the function already makes about `observed` itself: a
    relative path means whether two lanes share a tree depends on the cwd of
    whoever asks, and resolving it against this process's own would answer a
    question about a different pair of directories."""
    violations = validate_observed_checkout(
        tmp_path / "observed" / lane_id(0),
        tmp_path / "repo",
        tmp_path / ".al",
        tmp_path / "workers_root",
        [Path("observed/_lane-1")],
    )

    assert violations
    assert "is not an absolute path" in violations[0]


def test_at_one_lane_the_validator_answers_exactly_what_it_answered_before(tmp_path):
    """No sibling exists at one lane, so the new parameter cannot change an
    answer there — asserted rather than assumed, in both spellings (omitted,
    and an explicit empty list), against a good path and a bad one."""
    repo, state, workers = tmp_path / "repo", tmp_path / ".al", tmp_path / "workers_root"
    good = tmp_path / "observed"
    bad = workers / "inside"

    assert validate_observed_checkout(good, repo, state, workers) == []
    assert validate_observed_checkout(good, repo, state, workers, ()) == []
    assert validate_observed_checkout(good, repo, state, workers, []) == []
    assert validate_observed_checkout(bad, repo, state, workers) == (
        validate_observed_checkout(bad, repo, state, workers, ())
    )
    assert validate_observed_checkout(bad, repo, state, workers, ()), "still refused"


# =============================================================================
# 3. Real trees — the sibling sync, its control, the genuine escape, and where
#    a worker repository says it came from
# =============================================================================


def two_lanes(tmp_path):
    """A primary checkout and two lanes' clones of it, both synchronised to its
    head — the state every round starts from, at two lanes."""
    primary = make_repo_from_template(tmp_path / "repo")
    root = tmp_path / "observed"
    lane_a = ObservedCheckout(lane_observed_checkout(root, 0, 2))
    lane_b = ObservedCheckout(lane_observed_checkout(root, 1, 2))
    head = head_of(primary)
    assert lane_a.synchronize(primary, [head]) == []
    assert lane_b.synchronize(primary, [head]) == []
    return primary, lane_a, lane_b, head


def test_a_sibling_lane_synchronising_mid_window_is_no_violation_in_lane_a(tmp_path):
    """Part 1. Lane B does the most disruptive ordinary thing a lane does —
    checks its whole working tree out at a different commit — strictly inside
    lane A's window, and lane A's detector reports nothing."""
    primary, lane_a, lane_b, head = two_lanes(tmp_path)
    git_a, paths_before, before = open_window(lane_a)

    moved = commit_readme(primary, "the head moved under lane B\n")
    assert lane_b.synchronize(primary, [moved]) == []

    assert close_window(git_a, paths_before, before) == []
    # NOT VACUOUS: lane B really did rewrite its tree, and lane A really is
    # still at the commit its window was opened on.
    assert lane_b.head_sha() == moved
    assert lane_a.head_sha() == head
    assert (lane_b.path / "README.md").read_text(encoding="utf-8") == (
        "the head moved under lane B\n"
    )
    assert (lane_a.path / "README.md").read_text(encoding="utf-8") != (
        "the head moved under lane B\n"
    )


def test_the_control_one_shared_clone_reports_that_same_sync_against_lane_a(tmp_path):
    """The pair that makes the test above evidence rather than a restatement of
    the directory layout. Byte for byte the same synchronisation; the only
    difference is that both lanes are pointed at ONE tree — and lane A's window
    now reports lane B's routine work as an escape."""
    primary = make_repo_from_template(tmp_path / "repo")
    shared = tmp_path / "observed"
    lane_a = ObservedCheckout(shared)
    lane_b = ObservedCheckout(shared)   # the pre-conc-04 arrangement: one clone
    assert lane_a.synchronize(primary, [head_of(primary)]) == []

    git_a, paths_before, before = open_window(lane_a)
    moved = commit_readme(primary, "the head moved under lane B\n")
    assert lane_b.synchronize(primary, [moved]) == []

    violations = close_window(git_a, paths_before, before)
    assert violations, "a shared clone is exactly what Decision 1 refuses"
    assert any("README.md" in violation for violation in violations), violations


def test_a_write_into_lane_as_own_clone_during_its_window_is_still_reported(tmp_path):
    """Part 2, per lane: isolation must not have bought silence. Both shapes
    the detector has to keep catching — a new file nothing tracks, and an edit
    to a tracked one — and each is reported WITH ITS PATH, which is what makes
    the park nameable."""
    _primary, lane_a, lane_b, _head = two_lanes(tmp_path)
    git_a, paths_before, before = open_window(lane_a)

    (lane_a.path / "planted.py").write_text("evil\n", encoding="utf-8")
    (lane_a.path / "README.md").write_text("TAMPERED\n", encoding="utf-8")
    # ... while the sibling lane's tree, untouched by this agent, stays quiet.
    assert lane_b.residue() == []

    violations = close_window(git_a, paths_before, before)

    assert any("planted.py" in violation for violation in violations), violations
    assert any("README.md" in violation for violation in violations), violations


def test_lowering_the_cap_onto_a_root_holding_lane_clones_refuses_to_watch_them(tmp_path):
    """The one transition that could turn a sibling lane's ordinary work into
    THIS lane's escape, and the reason it cannot: an operator who lowers
    `[concurrency] lanes` back to 1 does not end the sessions in the lanes the
    new cap cut out, and lane 0 then resolves to the configured path — which by
    then CONTAINS those lanes' clones. If that were simply observed, lane 1's
    next synchronisation would land inside lane 0's window and be reported as
    lane 0's agent writing outside its worker repository.

    It is not observed. `synchronize` refuses the tree before a single byte is
    fetched or checked out, before any agent runs, and deletes nothing — the
    same fail-closed answer it gives for every other tree it cannot establish."""
    primary, lane_a, lane_b, head = two_lanes(tmp_path)
    root = lane_a.path.parent
    single_lane = ObservedCheckout(root)   # what lane 0 resolves to at `lanes = 1`

    violations = single_lane.synchronize(primary, [head])

    assert violations, "a root full of lane clones must never be observed as a tree"
    assert "not the top level of a git repository" in violations[0], violations
    assert lane_a.path.is_dir() and lane_b.path.is_dir(), "nothing here deletes a lane"
    assert lane_b.residue() == [], "and nothing here writes into one either"


def test_a_worker_repo_for_lane_a_records_lane_as_own_clone_in_fetch_head(tmp_path):
    """Part 3, the provenance half. The fetch source recorded in
    `.git/FETCH_HEAD` is the one absolute path to a non-worker tree that an
    agent inside a worker repo can read off disk, so at two lanes it must name
    the agent's OWN lane's clone — the tree that lane's detector brackets — and
    must not name the sibling's."""
    _primary, lane_a, lane_b, head = two_lanes(tmp_path)
    repos = WorkerRepoManager(tmp_path / "workers_root", tmp_path / "worker-hooks")

    worker = repos.create("t1", lane_a.path, head)

    fetch_head = (worker.path / ".git" / "FETCH_HEAD").read_text(encoding="utf-8")
    assert head in fetch_head, fetch_head
    assert str(lane_a.path) in fetch_head, fetch_head
    assert str(lane_b.path) not in fetch_head, fetch_head


# =============================================================================
# 4. The orchestrator routes every one of the three onto its own lane's clone
# =============================================================================


def build_orchestrator(tmp_path, lanes=1, lane_index=0, observed=None):
    config = a_config(tmp_path, lanes=lanes, observed=observed)
    return Orchestrator(
        config=config,
        store=StateStore(config.state_file),
        state=LoopState(session_id="conc-04", conversation_url=URL),
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: None,
        registry=TaskRegistry([]),
        task_store=TaskStore(config.tasks_file),
        manifest_store=ManifestStore(config.manifests_dir),
        observed_checkout=ObservedCheckout(observed) if observed else None,
        lane_index=lane_index,
    )


def test_at_one_lane_the_orchestrator_watches_the_tree_it_was_handed(tmp_path):
    """The acceptance criterion at the wiring: the object the caller built is
    the object the round uses, at the path the config named."""
    observed = tmp_path / "observed"
    orch = build_orchestrator(tmp_path, lanes=1, observed=observed)

    assert orch._observed.path == observed.resolve()
    assert orch._worker_fetch_root() == observed.resolve()
    assert orch._observation_git().repo_root == observed.resolve()


@pytest.mark.parametrize("lane_index", [0, 1])
def test_above_one_lane_all_three_routes_follow_that_lanes_own_clone(tmp_path, lane_index):
    """The snapshots, the fetch source and the tree the sync establishes are one
    directory per lane, or they are not isolation at all: a detector bracketing
    one tree while workers are seeded from another would watch a tree no agent
    was ever pointed at."""
    observed = tmp_path / "observed"
    orch = build_orchestrator(tmp_path, lanes=2, lane_index=lane_index, observed=observed)
    expected = (observed / lane_id(lane_index)).resolve()

    assert orch._observed.path == expected
    assert orch._worker_fetch_root() == expected
    assert orch._observation_git().repo_root == expected

    sibling = build_orchestrator(
        tmp_path, lanes=2, lane_index=1 - lane_index, observed=observed
    )
    assert sibling._observed.path != orch._observed.path
    assert validate_observed_checkout(
        orch._observed.path,
        tmp_path / "repo",
        tmp_path / ".al",
        tmp_path / "workers_root",
        [sibling._observed.path],
    ) == []


def test_a_deployment_with_no_clone_wired_is_untouched_by_the_lane_rule(tmp_path):
    """`None` stays `None` at every lane index: the pre-esc-02 deployment
    watches the primary checkout, and there is no path to make per-lane."""
    orch = build_orchestrator(tmp_path, lanes=2, lane_index=1, observed=None)

    assert orch._observed is None
    assert orch._lane_isolation_violations() == [], "nothing to isolate from"


# =============================================================================
# 5. The rule asked where it can still stop something — and the symlink that
#    makes the derivation alone insufficient
# =============================================================================
#
# `config.lane_observed_checkout` makes two lanes' NAMES distinct. A symlink
# makes two distinct names one directory, and a lane set that resolves onto one
# tree is exactly the arrangement Decision 1 removes — so the rule has to be
# asked about RESOLVED paths, by the round, before the tree is written to. These
# are the tests for that caller: the previous section pins the rule, this one
# pins that production reaches it.


class _CountingExecutor:
    """Runs nothing. What is under test is whether it is called at all."""

    def __init__(self):
        self.calls = 0

    def execute(self, directive, task):
        self.calls += 1
        return ExecutionOutcome(
            status="ok", summary="did it", validation="ok", changed_paths=()
        )


class _RecordingWorkerRepos(WorkerRepoManager):
    """Records the fetch source every worker repository is seeded from — the
    second thing that must not happen when a lane's clone is refused."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sources: list[Path] = []

    def create(self, task_id, source_repo_path, base_sha, branch=None):
        self.sources.append(Path(source_repo_path).resolve())
        return super().create(task_id, source_repo_path, base_sha, branch=branch)


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def implement(task_id="t1") -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="go", task_id=task_id)


def a_task(task_id="t1") -> Task:
    return Task(id=task_id, title="T", description="d", approved_paths=("feature.py",))


def lane_layout(tmp_path):
    """A primary checkout and ONE real clone, at lane 1's derived path. Lane 0's
    directory is left free for whatever each test below plants there."""
    primary = make_repo_from_template(tmp_path / "repo")
    root = tmp_path / "observed"
    root.mkdir(parents=True)
    head = head_of(primary)
    lane_b = ObservedCheckout(lane_observed_checkout(root, 1, 2))
    assert lane_b.synchronize(primary, [head]) == []
    return primary, root, lane_b, head


def dispatching_orchestrator(tmp_path, root, primary, *, lanes, lane_index):
    """A lane's orchestrator wired the way a real round is — a primary checkout,
    a worker-repo manager, an executor — so a refusal can be asked the only
    question that matters about it: did anything run?"""
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers_root",
        observed_checkout=root,
        concurrency=ConcurrencyConfig(lanes=lanes),
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    registry = TaskRegistry([a_task()])
    task_store = TaskStore(
        config.tasks_file,
        ledger=mutation_ledger_for(config.workers_root, config.state_dir),
    )
    task_store.save(registry)
    executor = _CountingExecutor()
    repos = _RecordingWorkerRepos(config.workers_root, tmp_path / "worker-hooks")
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=GitGateway(primary, PolicyEngine(config.policy)),
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: None,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=repos,
        execution_store=TaskExecutionStore(tmp_path / "executions"),
        intent_store=IntentStore(tmp_path / "intents"),
        validation_runner=ok_validation,
        observed_checkout=ObservedCheckout(root),
        lane_index=lane_index,
    )
    return orch, executor, repos


def test_two_lanes_symlinked_onto_one_tree_park_before_the_clone_is_touched(tmp_path):
    """The layout the derivation cannot rule out: `_lane-0` is a symlink to
    `_lane-1`, so two lexically distinct lane paths are ONE working tree and
    lane 0 would bracket a tree lane 1 synchronises. The round refuses, and it
    refuses BEFORE the clone is written to — which is the only refusal worth
    having, because a `synchronize` that had already run would have rewritten
    the sibling's tree inside the sibling's window."""
    primary, root, lane_b, head = lane_layout(tmp_path)
    (root / lane_id(0)).symlink_to(lane_b.path, target_is_directory=True)
    orch, executor, repos = dispatching_orchestrator(
        tmp_path, root, primary, lanes=2, lane_index=0
    )
    assert orch._observed.path == lane_b.path, "not vacuous: it really is one tree"

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 0, "no agent may run in a tree another lane watches"
    assert repos.sources == [], "and no worker repository may be seeded from one"
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    question = orch.state.question or ""
    assert "IS another lane's observed checkout" in question, question
    # BEFORE, not after: lane 1's tree is byte for byte what it was, so nothing
    # was fetched into it, checked out in it, or left behind in it.
    assert lane_b.head_sha() == head
    assert lane_b.residue() == []


def test_a_lane_symlinked_inside_a_sibling_is_refused_from_both_sides(tmp_path):
    """The nested spelling of the same defeat, and both directions of it —
    either lane may be the one that reaches its boundary first, and a rule that
    only looked downwards would let the containing lane carry on bracketing a
    tree its sibling writes to."""
    primary, root, lane_b, head = lane_layout(tmp_path)
    inner = lane_b.path / "inner"
    inner.mkdir()
    (root / lane_id(0)).symlink_to(inner, target_is_directory=True)

    lane_a_orch, executor_a, repos_a = dispatching_orchestrator(
        tmp_path, root, primary, lanes=2, lane_index=0
    )
    lane_b_orch, _executor_b, _repos_b = dispatching_orchestrator(
        tmp_path, root, primary, lanes=2, lane_index=1
    )
    assert lane_a_orch._observed.path == inner.resolve(), "one tree inside another"

    a_violations = lane_a_orch._lane_isolation_violations()
    assert a_violations, "a lane inside a sibling's clone is refused"
    assert "is nested beneath another lane's observed checkout" in a_violations[0]
    b_violations = lane_b_orch._lane_isolation_violations()
    assert b_violations, "and so is the sibling that contains it"
    assert "is nested beneath observed_checkout" in b_violations[0]

    lane_a_orch._dispatch_executor(implement("t1"))

    assert executor_a.calls == 0
    assert repos_a.sources == []
    assert lane_a_orch.state.park_kind == "loop_fatal"
    assert lane_b.head_sha() == head, "the sibling's tree was never established"


def test_at_one_lane_the_check_compares_nothing_and_the_round_carries_on(tmp_path):
    """The acceptance criterion at the new caller. One lane has no siblings, so
    the comparison is over an empty set, no branch it could park on is reachable,
    and the boundary does exactly what it did before this existed: it
    synchronises the configured tree and returns True."""
    primary = make_repo_from_template(tmp_path / "repo")
    observed = tmp_path / "observed"
    orch, _executor, _repos = dispatching_orchestrator(
        tmp_path, observed, primary, lanes=1, lane_index=0
    )

    assert orch._observed.path == observed.resolve(), "exactly the configured path"
    assert orch._lane_isolation_violations() == []
    assert orch._synchronise_observed_checkout(a_task()) is True
    assert orch.state.phase != Phase.NEEDS_USER.value
    assert ObservedCheckout(observed).head_sha() == head_of(primary)


def test_a_retired_lane_at_one_lane_is_still_compared_against_lane_zero(tmp_path):
    """Where "at one lane nothing is compared" stops being the whole truth,
    stated rather than left to be found. Lowering `[concurrency] lanes` to 1 does
    not end the session in lane 1, and lane 0 then resolves to the root that
    CONTAINS lane 1's clone — two trees that nest, with no attribution between
    them. Lane 0's own round already refuses that root from the other side
    (`synchronize`: not the top level of a git repository); this is the same
    refusal asked by the lane inside it. The acceptance criterion sits in the
    same test, one line up: the only lane a single-lane deployment HAS compares
    nothing."""
    observed = tmp_path / "observed"

    lane_zero = build_orchestrator(tmp_path, lanes=1, lane_index=0, observed=observed)
    assert lane_zero._lane_isolation_violations() == []

    retired = build_orchestrator(tmp_path, lanes=1, lane_index=1, observed=observed)
    violations = retired._lane_isolation_violations()
    assert violations, "a lane whose clone sits inside lane 0's tree is refused"
    assert "nested beneath another lane's observed checkout" in violations[0]


def test_an_isolation_check_that_cannot_answer_refuses_rather_than_passing(tmp_path):
    """The fail-open this guard is shaped against. "I could not work out whether
    two lanes share a tree" must never be reported as "they do not" — the alarm
    that silently switches itself off when its input is unreadable. Provoked
    through the fleet size the sibling set is derived from, because that is the
    one input to the derivation this process cannot re-read or repair."""
    primary, root, lane_b, head = lane_layout(tmp_path)
    orch, executor, repos = dispatching_orchestrator(
        tmp_path, root, primary, lanes=2, lane_index=1
    )
    assert orch._lane_isolation_violations() == [], (
        "not vacuous: this layout is the isolated one, and it answers cleanly"
    )

    orch._observed_lanes = "2"      # a fleet size no derivation can read

    violations = orch._lane_isolation_violations()
    assert violations and "could not be determined" in violations[0], violations

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 0, "an unanswerable check runs nothing"
    assert repos.sources == []
    assert orch.state.park_kind == "loop_fatal"
    assert lane_b.head_sha() == head


def test_an_orchestrator_assembled_without_init_presents_as_one_lane(tmp_path):
    """`Orchestrator.__new__` plus a handful of attributes is how several tests
    in this suite build the carry-forward path, and such an object records
    neither a fleet size nor the deployment path a fleet's lanes derive from. It
    is lane 0 of a fleet of one: it compares nothing, which is the answer the
    single-lane loop gives, rather than raising on an attribute it never set.
    The one incoherent pair is still refused — a fleet of more than one with
    nothing to derive its lanes from cannot be answered, and is not passed."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._observed = ObservedCheckout(tmp_path / "observed")

    assert orch._lane_isolation_violations() == []

    orch._observed_lanes = 2

    violations = orch._lane_isolation_violations()
    assert violations, "a fleet with nothing to derive its lanes from is refused"
    assert "cannot be answered" in violations[0], violations


def test_the_sibling_rule_is_one_rule_asked_from_two_places(tmp_path):
    """The extracted function and the validator's sibling half are the same
    code, not two copies that agree today — asserted as an equality over every
    arrangement the rule distinguishes, so a future edit to either has to move
    both or fail here."""
    root = tmp_path / "observed"
    lane_a = lane_observed_checkout(root, 0, 2)
    repo, state, workers = tmp_path / "repo", tmp_path / ".al", tmp_path / "workers_root"

    for observed, others in (
        (lane_a, [lane_observed_checkout(root, 1, 2)]),   # isolated
        (lane_a, [lane_a]),                               # the same tree
        (lane_a, [root]),                                 # inside a sibling
        (root, [lane_a]),                                 # containing one
        (lane_a, [Path("observed/_lane-1")]),             # a relative sibling
    ):
        assert validate_observed_checkout(observed, repo, state, workers, others) == (
            validate_lane_checkout_distinctness(observed, others)
        ), (observed, others)

    # And the inputs that reach no comparison at all: the two the validator
    # refuses BEFORE this rule (so it never answers them in a second voice), a
    # sibling lane that watches nothing, and no siblings at all.
    assert validate_lane_checkout_distinctness(None, [lane_a]) == []
    assert validate_lane_checkout_distinctness(Path("relative"), [lane_a]) == []
    assert validate_lane_checkout_distinctness(lane_a, [None]) == []
    assert validate_lane_checkout_distinctness(lane_a) == []
