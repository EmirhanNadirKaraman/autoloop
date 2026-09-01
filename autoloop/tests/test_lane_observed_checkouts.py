"""conc-04 — one observed checkout per lane, and what that buys.

THE CLAIM THESE TESTS EXIST TO PIN, in the three parts the split plan states
it in (docs/AUTOLOOP.md, "Running several tasks at once", Decision 1):

  1. each lane's escape detector brackets a tree only that lane writes — so
     lane B synchronising its own clone INSIDE lane A's window produces no
     violation in lane A, where one shared clone would have reported every
     path in the repository;
  2. a genuine escape inside a lane is still detected, with its path, and now
     also with the lane it happened in;
  3. a write by one lane is not attributed to another — which rests on the
     FETCH_HEAD property esc-02 established, now per lane: the one absolute
     path to a non-worker tree that a worker repository carries on disk names
     THAT lane's watched clone.

Plus the acceptance criterion every candidate in that plan carries: at
`lanes = 1` the clone path is exactly `[paths].observed_checkout` as resolved
today, so nothing in the existing suite needed editing to admit this.

WHAT THIS MODULE ASSERTS THAT `test_observed_checkout.py` DELIBERATELY DOES
NOT: the bytes of `.git/FETCH_HEAD`. That module records the ARGUMENT
`WorkerRepoManager.create` receives and says the spelling git chooses is not a
contract this repository controls, which is right for the claim it was pinning.
The plan names the FETCH_HEAD test for this candidate specifically, and the
reason is the residual it accepts: an agent that goes looking for "the repo"
reads that file, so which lane's path is IN IT is the whole bound on
cross-lane attribution. Read defensively — the source is matched at the END of
each record rather than by substring, because `<base>` is a prefix of
`<base>-lane-1` and a substring test would pass for the wrong lane.

Self-contained per this codebase's convention: real git repositories, no
fixtures imported from another test module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitrepo import make_repo_from_template, run_git

from autoloop.config import (
    AutoloopConfig,
    BrowserConfig,
    lane_observed_checkout,
    load_config,
    resolve_observed_checkout,
)
from autoloop.contract import Decision, Directive
from autoloop.errors import ConfigError
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
    validate_lane_observed_checkouts,
    validate_observed_checkout,
)
from autoloop.worktask import IntentStore, TaskExecutionStore

URL = "https://chatgpt.com/c/conc-04-lane-observed-checkouts"

#: What both trees ignore — the two artefacts that actually parked this loop.
GITIGNORE = "__pycache__/\n*.py[cod]\n.ruff_cache/\n"


def real_repo(tmp_path, name="repo") -> Path:
    return make_repo_from_template(
        tmp_path / name,
        files=(("README.md", "hello\n"), (".gitignore", GITIGNORE)),
    )


def head_of(repo_root: Path) -> str:
    return run_git(repo_root, "rev-parse", "HEAD").strip()


def commit_in(repo_root: Path, rel: str, body: str) -> str:
    (repo_root / rel).write_text(body, encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", f"add {rel}")
    return head_of(repo_root)


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def implement(task_id="t1") -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="go", task_id=task_id)


def inside(candidate: Path, boundary: Path) -> bool:
    """Pure path arithmetic — no filesystem — for "is one lane's tree in the
    other's". The same question `worker_env._is_nested` answers, asked here
    directly so the distinctness claim does not depend on the validator that
    is itself under test two functions down."""
    try:
        Path(candidate).relative_to(Path(boundary))
        return True
    except ValueError:
        return False


# =============================================================================
# 1. Where a lane's clone lands — the resolution rule, and its refusals
# =============================================================================


def test_two_lanes_resolve_to_different_directories_neither_inside_the_other(tmp_path):
    """Part 1's precondition. `observed-checkout/<lane_id>` — the other
    spelling the plan's prose offers — would put lane 1 INSIDE lane 0's tree,
    which the rule three paragraphs later in the same section forbids and which
    would rebuild the cross-attribution this candidate removes."""
    base = tmp_path / "observed"
    zero, one, two = (lane_observed_checkout(base, k) for k in (0, 1, 2))

    assert zero == base, "lane 0 is the configured path itself"
    assert len({zero, one, two}) == 3, (zero, one, two)
    for a in (zero, one, two):
        for b in (zero, one, two):
            if a != b:
                assert not inside(a, b), f"{a} sits inside {b}"
    # Deterministic, so two readers asking the same question agree.
    assert lane_observed_checkout(base, 1) == one


def test_the_validator_refuses_a_lane_nested_in_another_and_two_lanes_on_one_tree(tmp_path):
    """The one rule the plan says this candidate must add, in all three shapes
    it can arrive in: nested one way, nested the other way, and identical."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    state_dir = tmp_path / "state"
    workers_root = tmp_path / "workers"
    outer = tmp_path / "observed"
    inner = outer / "lane-1"

    nested = validate_observed_checkout(
        inner, repo_root, state_dir, workers_root, other_lane_checkouts=[outer]
    )
    assert nested and "nested beneath another lane's observed checkout" in nested[0]

    contains = validate_observed_checkout(
        outer, repo_root, state_dir, workers_root, other_lane_checkouts=[inner]
    )
    assert contains, "the containing direction matters as much as the nested one"
    assert "nested beneath observed_checkout" in contains[0]

    same = validate_observed_checkout(
        outer, repo_root, state_dir, workers_root, other_lane_checkouts=[Path(str(outer))]
    )
    assert same and "IS another lane's observed checkout" in same[0]

    # A sibling that cannot be compared at all is refused, never ignored: a
    # relative path resolves against whatever cwd the reader happens to have.
    relative = validate_observed_checkout(
        outer, repo_root, state_dir, workers_root, other_lane_checkouts=[Path("lanes/1")]
    )
    assert relative and "not an absolute path" in relative[0]


def test_two_lane_paths_that_are_one_directory_through_a_symlink_are_refused(tmp_path):
    """Distinct SPELLINGS are not distinct trees. The check resolves both
    sides, so a lane path symlinked onto another lane's clone is caught as the
    shared tree it is — the shape a lexical comparison would pass."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    base = tmp_path / "observed"
    base.mkdir()
    linked = lane_observed_checkout(base, 1)
    linked.symlink_to(base, target_is_directory=True)

    violations = validate_lane_observed_checkouts(
        [base, linked], repo_root, tmp_path / "state", tmp_path / "workers"
    )
    assert len(violations) == 2, violations
    assert all("IS another lane's observed checkout" in v for v in violations), violations


def test_a_real_two_lane_fleet_passes_and_a_hand_built_bad_one_does_not(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    state_dir = tmp_path / "state"
    workers_root = tmp_path / "workers"
    base = tmp_path / "observed"
    fleet = [lane_observed_checkout(base, k) for k in range(3)]

    assert validate_lane_observed_checkouts(fleet, repo_root, state_dir, workers_root) == []

    # Every message names its lane — "one of these contains another" is not an
    # answer an operator can act on.
    bad = validate_lane_observed_checkouts(
        [base, base / "one"], repo_root, state_dir, workers_root
    )
    assert bad and all(v.startswith("lane ") for v in bad), bad
    assert any(v.startswith("lane 0:") for v in bad) and any(v.startswith("lane 1:") for v in bad)


def test_the_fleet_check_runs_the_full_boundary_test_on_every_derived_path(tmp_path):
    """Not just pairwise nesting. A base that passes on its own can derive a
    sibling that swallows `workers_root` — checking only the configured path
    would have called this fleet safe."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    state_dir = tmp_path / "state"
    base = tmp_path / "observed"
    workers_root = lane_observed_checkout(base, 1) / "workers"

    assert validate_observed_checkout(base, repo_root, state_dir, workers_root) == [], (
        "not vacuous: the CONFIGURED path is perfectly fine on its own"
    )
    violations = validate_lane_observed_checkouts(
        [lane_observed_checkout(base, k) for k in (0, 1)],
        repo_root,
        state_dir,
        workers_root,
    )
    assert violations and any("the workers root" in v for v in violations), violations
    assert violations[0].startswith("lane 1:"), violations


@pytest.mark.parametrize("lane", [True, False, -1, "1", 1.0, None])
def test_a_lane_index_that_cannot_be_read_is_refused_not_treated_as_lane_zero(tmp_path, lane):
    """The fail-open this resolver must not have. Falling through to the lane-0
    branch would hand a second lane lane 0's own clone — two lanes, one tree,
    and every write in it attributable to neither. `True` is in the list
    because `isinstance(True, int)` is true and `True == 1`."""
    with pytest.raises(ConfigError, match="lane index"):
        lane_observed_checkout(tmp_path / "observed", lane)


def test_a_lane_above_zero_with_no_configured_clone_is_refused(tmp_path):
    """`None` means "watch the primary checkout" — safe for one lane, and for
    several it is every lane watching one tree nobody wrote to on purpose."""
    assert lane_observed_checkout(None, 0) is None
    with pytest.raises(ConfigError, match="no observed checkout"):
        lane_observed_checkout(None, 1)


def test_a_fleet_with_an_unconfigured_lane_is_refused_by_the_validator_too(tmp_path):
    """The same refusal one layer down, for a fleet assembled without going
    through the resolver at all."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    single = validate_lane_observed_checkouts(
        [None], repo_root, tmp_path / "state", tmp_path / "workers"
    )
    assert single == [], "one lane with no clone is the pre-esc-02 deployment"

    fleet = validate_lane_observed_checkouts(
        [None, tmp_path / "observed"], repo_root, tmp_path / "state", tmp_path / "workers"
    )
    assert fleet and fleet[0].startswith("lane 0:")
    assert "no observed checkout is configured" in fleet[0]


def test_at_one_lane_the_clone_path_is_exactly_the_configured_observed_checkout(tmp_path):
    """The acceptance criterion every candidate carries, at the layer this one
    changes: nothing about lane 0's path moves, derived or explicit."""
    workers = tmp_path / "nest" / "workers"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n[paths]\nworkers_root = "{workers}"\n',
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.observed_checkout_for_lane(0) == config.observed_checkout
    assert config.observed_checkout_for_lane(0) == resolve_observed_checkout(None, workers)

    elsewhere = tmp_path / "elsewhere"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n[paths]\n'
        f'workers_root = "{workers}"\nobserved_checkout = "{elsewhere}"\n',
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.observed_checkout_for_lane(0) == elsewhere
    assert config.observed_checkout == elsewhere, "the configured value is untouched"


def test_the_sibling_argument_is_absent_by_default_so_every_old_caller_agrees(tmp_path):
    """`cli._build_orchestrator` and `doctor` call this with four arguments and
    must keep getting the pre-conc-04 answer."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    assert validate_observed_checkout(
        tmp_path / "observed", repo_root, tmp_path / "state", tmp_path / "workers"
    ) == []


# =============================================================================
# 2. End to end — two lanes, one primary checkout, one window at a time
# =============================================================================


class _HookedExecutor:
    """Writes into its own worker repo (legitimate) and runs `hook` inside the
    window — either a sibling lane's synchronisation, or a genuine escape into
    this lane's own clone, depending on what the test hands it."""

    def __init__(self, workers_root, hook):
        self.workers_root = Path(workers_root)
        self.hook = hook
        self.calls = 0

    def execute(self, directive, task):
        self.calls += 1
        (self.workers_root / task.id / "feature.py").write_text(
            "print('hi')\n", encoding="utf-8"
        )
        self.hook()
        return ExecutionOutcome(
            status="ok", summary="did it", validation="ok", changed_paths=("feature.py",)
        )


def _lane(tmp_path, repo_root, index, hook, *, clone=None, lane_id=None, task_id="t1"):
    """One lane: its own state dir, its own worker repositories, its own clone
    (derived by the rule under test), its own Orchestrator.

    `clone` overrides the derived path — the shared-clone control below is
    exactly the arrangement this candidate replaces, and it must be reachable
    from this same builder or the comparison would be between two different
    test harnesses rather than between two isolation boundaries.
    """
    lane_root = tmp_path / f"lane-{index}"
    workers_root = lane_root / "workers_root"
    observed = clone if clone is not None else lane_observed_checkout(
        tmp_path / "observed", index
    )
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=lane_root / "state",
        workers_root=workers_root,
        observed_checkout=observed,
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    task = Task(id=task_id, title="T", description="d", approved_paths=("feature.py",))
    registry = TaskRegistry([task])
    task_store = TaskStore(
        config.tasks_file,
        ledger=mutation_ledger_for(config.workers_root, config.state_dir),
    )
    task_store.save(registry)
    executor = _HookedExecutor(workers_root, hook)

    def no_client():
        raise AssertionError("no browser client expected in this test")

    kwargs = {} if lane_id is None else {"lane_id": lane_id}
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=GitGateway(repo_root, PolicyEngine(PolicyConfig(implement_enabled=True))),
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=WorkerRepoManager(workers_root, lane_root / "worker-hooks"),
        execution_store=TaskExecutionStore(lane_root / "executions"),
        intent_store=IntentStore(lane_root / "intents"),
        validation_runner=ok_validation,
        observed_checkout=ObservedCheckout(observed),
        **kwargs,
    )
    return orch, executor, ObservedCheckout(observed), workers_root


def test_a_sibling_lane_synchronising_inside_the_window_is_no_violation(tmp_path):
    """Part 1, and the round that would have been loop-fatal on the first
    overlap. Lane B moves its own clone to a commit lane A has never seen,
    while lane A's window is open. `ObservedCheckout.synchronize` rewrites a
    whole working tree, so on a shared clone this reports every path in the
    repository."""
    repo_root = real_repo(tmp_path)
    lane_b_clone = lane_observed_checkout(tmp_path / "observed", 1)
    lane_b = ObservedCheckout(lane_b_clone)
    assert lane_b.synchronize(repo_root, [head_of(repo_root)]) == []
    moved: dict = {}

    def hook():
        # A commit lane A's clone does not hold, and lane B moving onto it.
        moved["sha"] = commit_in(repo_root, "mainline.txt", "somebody else shipped\n")
        moved["violations"] = lane_b.synchronize(repo_root, [moved["sha"]])

    orch, executor, lane_a, _workers = _lane(tmp_path, repo_root, 0, hook, lane_id="0")

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1, "not vacuous: the round really ran"
    assert moved["violations"] == [], "lane B's own synchronisation must succeed"
    assert lane_b.head_sha() == moved["sha"], "lane B really moved inside the window"
    assert (lane_b_clone / "mainline.txt").exists(), (
        "not vacuous: lane B's WORKING TREE really was rewritten inside the window"
    )
    assert not (lane_a.path / "mainline.txt").exists(), "lane A's tree is its own"
    question = orch.state.question or ""
    assert "outside its worker repository" not in question, question
    assert orch.state.phase != Phase.NEEDS_USER.value, question


def test_the_same_synchronisation_on_a_SHARED_clone_parks_the_round(tmp_path):
    """The control that makes the test above mean something: byte for byte the
    same sibling synchronisation, with the only difference being that both
    lanes were pointed at one tree. This is the arrangement Decision 1
    replaces, and it fails exactly as the plan predicts."""
    repo_root = real_repo(tmp_path)
    shared = tmp_path / "observed"
    lane_b = ObservedCheckout(shared)
    assert lane_b.synchronize(repo_root, [head_of(repo_root)]) == []

    def hook():
        sha = commit_in(repo_root, "mainline.txt", "somebody else shipped\n")
        lane_b.synchronize(repo_root, [sha])

    orch, executor, _lane_a, _workers = _lane(
        tmp_path, repo_root, 0, hook, clone=shared, lane_id="0"
    )

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1
    assert orch.state.park_kind == "loop_fatal"
    question = orch.state.question or ""
    assert "outside its worker repository" in question, question
    assert "mainline.txt" in question, question


def test_a_genuine_escape_inside_a_lane_is_still_reported_with_its_path(tmp_path):
    """Part 2. Isolating the window must not have narrowed it: a write into
    THIS lane's clone — tracked, untracked and ignored alike — is still
    loop-fatal, still names the path, and now also names the lane."""
    repo_root = real_repo(tmp_path)
    holder: dict = {}

    def hook():
        clone = holder["clone"]
        (clone / "planted.py").write_text("evil\n", encoding="utf-8")
        (clone / "README.md").write_text("TAMPERED\n", encoding="utf-8")
        cache = clone / ".ruff_cache" / "0.14.1"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "0123456789abcdef").write_bytes(b"cache")

    orch, executor, lane_a, _workers = _lane(tmp_path, repo_root, 1, hook, lane_id="1")
    holder["clone"] = lane_a.path

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1
    assert orch.state.park_kind == "loop_fatal"
    question = orch.state.question or ""
    for expected in ("planted.py", "README.md", ".ruff_cache/0.14.1/0123456789abcdef"):
        assert expected in question, (expected, question)
    # ATTRIBUTED: the park says which lane's tree it was.
    assert "[lane 1]" in question, question
    assert run_git(lane_a.path, "status", "--porcelain").strip() != ""


def test_without_a_lane_id_the_park_keeps_todays_wording(tmp_path):
    """The `lanes = 1` criterion applied to the message text. Every existing
    test that matches on this park's wording is constructed exactly this way —
    no lane id — so nothing about it may move until a fleet exists."""
    repo_root = real_repo(tmp_path)
    holder: dict = {}

    def hook():
        (holder["clone"] / "planted.py").write_text("evil\n", encoding="utf-8")

    orch, executor, lane_a, _workers = _lane(tmp_path, repo_root, 0, hook)
    holder["clone"] = lane_a.path

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1
    question = orch.state.question or ""
    assert "the OBSERVED checkout (" in question and ") outside its worker" in question
    assert "[lane" not in question, question


def _fetch_head_sources(worker_path: Path) -> list[str]:
    """Every fetch source `.git/FETCH_HEAD` records, one per line.

    Matched at the END of the record rather than by substring, because
    `<base>` is a literal prefix of `<base>-lane-1`: a substring test would
    report lane 0's clone as present in a worker seeded from lane 1's.
    """
    text = (worker_path / ".git" / "FETCH_HEAD").read_text(encoding="utf-8")
    return [line.split("\t")[-1].strip() for line in text.splitlines() if line.strip()]


def test_a_worker_repo_for_a_lane_records_that_lanes_clone_in_fetch_head(tmp_path):
    """Part 3, at the one place an absolute path to a non-worker tree actually
    leaks into a worker repository. An agent that goes looking for "the repo"
    reads this file; what it finds has to be its OWN lane's watched tree, or
    the residual the plan accepts (a write into a sibling's tree is attributed
    to the sibling) would be the ordinary case rather than the exotic one."""
    repo_root = real_repo(tmp_path)
    lane_b_clone = lane_observed_checkout(tmp_path / "observed", 1)
    lane_b = ObservedCheckout(lane_b_clone)
    assert lane_b.synchronize(repo_root, [head_of(repo_root)]) == []

    orch, executor, lane_a, workers_root = _lane(
        tmp_path, repo_root, 0, lambda: None, lane_id="0"
    )

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1, "not vacuous: the round really ran"
    assert (workers_root / "t1").is_dir(), "no worker repository was created at all"
    sources = _fetch_head_sources(workers_root / "t1")
    assert sources, "the worker repo recorded no fetch source"
    assert any(src.endswith(str(lane_a.path)) for src in sources), sources
    assert not any(src.endswith(str(lane_b.path)) for src in sources), sources
    assert not any(src.endswith(str(repo_root.resolve())) for src in sources), sources
    assert str(lane_a.path) in (
        (workers_root / "t1" / ".git" / "FETCH_HEAD").read_text(encoding="utf-8")
    )


# =============================================================================
# 3. The cached observation gateway follows the lane's clone
# =============================================================================


def test_the_observation_gateway_re_roots_when_the_lanes_clone_is_replaced(tmp_path):
    """The fail-open a cache keyed on "was one built" would have. A fleet that
    recovers one lane by handing it a rebuilt clone (Decision 8) would
    otherwise keep snapshotting through a gateway rooted at the tree the lane
    no longer owns — and both snapshots would agree, so the diff would be
    empty, the park would never fire and nothing would say so."""
    repo_root = real_repo(tmp_path)
    orch = Orchestrator.__new__(Orchestrator)
    orch._policy = PolicyEngine(PolicyConfig())
    orch._git = GitGateway(repo_root, orch._policy)
    orch._observed = ObservedCheckout(lane_observed_checkout(tmp_path / "observed", 0))
    orch._observed_git = None

    first = orch._observation_git()
    assert first.repo_root == orch._observed.path
    assert orch._observation_git() is first, "still cached while the path holds"

    orch._observed = ObservedCheckout(lane_observed_checkout(tmp_path / "observed", 1))
    second = orch._observation_git()
    assert second is not first
    assert second.repo_root == orch._observed.path

    # And with no clone at all it is still the primary checkout, unchanged.
    orch._observed = None
    assert orch._observation_git().repo_root == repo_root
