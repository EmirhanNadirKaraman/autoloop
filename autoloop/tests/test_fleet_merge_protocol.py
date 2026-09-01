"""conc-03: the rebase-aware, re-reviewed, serialised merge.

Candidate 2 of the nine in docs/AUTOLOOP.md's "Running several tasks at once —
the split plan". THE CLAIM: a candidate whose base moved is carried forward and
re-reviewed, never merged or pushed on its old approval; and at `lanes = 1` the
merge window is byte-identical to today's.

Everything here comes in pairs. Each fleet behaviour is asserted at `lanes > 1`
AND asserted absent at `lanes = 1`, because "changes nothing at one lane" is the
acceptance criterion every candidate in that plan carries and an untested half
of it is exactly how the criterion rots. The lane count is set with
`object.__setattr__` on a frozen `AutoloopConfig`: conc-02 owns the real
`[concurrency]` section and has not landed yet, so this stands in for the field
it will add — `auto_merge.concurrency_lanes` reads either shape and answers 1
for a config that has neither, which is what every test here depends on.

Real git wherever the claim is about git (the carry-forward merges, the sweep's
three branches), and a stub gateway where the claim is about a predicate over
records — see `test_postcommit_primitives.py` for why the small helpers below
are duplicated rather than imported.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from gitrepo import make_repo_from_template

from autoloop import auto_merge, cli, merge_sweep
from autoloop.auto_merge import MergeDeferralStore, concurrency_lanes
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import GitCommandError
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyEngine
from autoloop.state import LastResponse, LoopState, Phase, PostcommitBinding, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/conc-03"
BASE = "work"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def head_of(repo) -> str:
    return run_git(repo, "rev-parse", "HEAD")


def contains(repo, tip, sha) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, tip], cwd=str(repo), capture_output=True
    ).returncode == 0


def set_lanes(config, lanes):
    """What conc-02's `[concurrency] lanes` will do to a loaded config.

    `object.__setattr__` because `AutoloopConfig` is frozen and does not carry
    the field yet. Deliberately a plain attribute rather than a monkeypatch of
    `concurrency_lanes`: patching the reader would test these paths with the
    mechanism they are gated on switched off.
    """
    object.__setattr__(config, "lanes", lanes)
    return config


# =============================================================================
# 1. the lane count itself — every unusable answer is ONE lane
# =============================================================================


def test_a_config_that_names_no_lanes_is_one_lane():
    assert concurrency_lanes(None) == 1
    assert concurrency_lanes(SimpleNamespace()) == 1


def test_either_shape_of_the_setting_is_read():
    """conc-03 cannot see whether conc-02 lands `[concurrency] lanes` as a
    section object or as a flat field, and reading only the other one would pin
    the whole fleet at 1 with every test still green."""
    assert concurrency_lanes(SimpleNamespace(lanes=4)) == 4
    assert concurrency_lanes(SimpleNamespace(concurrency=SimpleNamespace(lanes=3))) == 3


@pytest.mark.parametrize("value", [0, -1, "two", 2.0, None, True, False, [2]])
def test_an_unusable_lane_setting_reads_as_one_lane(value):
    """FAIL CLOSED, and this is the direction that matters: reading garbage as a
    fleet would open the merge window on a loop whose window has always been
    shut. `load_config` refuses these values outright; this is what happens if
    one reaches here anyway."""
    assert concurrency_lanes(SimpleNamespace(lanes=value)) == 1


# =============================================================================
# 2. the merge window — shut at one lane, an OBLIGATION above one
# =============================================================================


class StubWindowGit:
    """The two local questions `_candidate_base_ancestry` asks, and nothing
    else. `place` False makes `merge-base` fail the way a checkout that has
    never heard of the sha does — the `BASE_UNVERIFIED` shape."""

    def __init__(self, head, place=True):
        self._head = head
        self._place = place

    def head_sha(self):
        return self._head

    def is_descendant(self, descendant, ancestor):
        if not self._place:
            raise GitCommandError("merge-base", f"{ancestor}: not a valid object name")
        return True

    def remote_ref_sha(self, remote, dest_ref):  # pragma: no cover - never reached
        raise AssertionError("a record with no push intent must not reach the remote")


@pytest.fixture
def window(tmp_path):
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / "repo" / ".autoloop",
        workers_root=tmp_path / "workers",
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def bound_record(config, *, task_id="t-1", candidate="abc123def456", base="000111222333"):
    """An unpublished candidate bound to a base — the record the window is
    about. No `intended_remote`, so `_candidate_publication` answers "never
    pushed" without a round-trip."""
    directory = config.state_dir / "executions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{task_id}.json").write_text(
        json.dumps({
            "task_id": task_id,
            "candidate_sha": candidate,
            "task_base_sha": base,
            "worktree_path": "",
        }),
        encoding="utf-8",
    )


def test_at_one_lane_a_candidate_bound_to_the_head_shuts_the_window(window):
    """TODAY'S PREDICATE AND TODAY'S WORDING, asserted as one whole string
    rather than by substring: the acceptance criterion is byte-identical
    behaviour at one lane, and a reason that merely still mentions the task
    would pass a weaker test."""
    bound_record(window, base="000111222333")
    git = StubWindowGit("000111222333")

    reasons, notes = cli._merge_window_blockers(window, set(), git)

    assert reasons == [
        "task t-1 has a candidate (abc123def456) bound to base 000111222333 — "
        "never pushed; that base IS the current head 000111222333, so merging "
        "would strand it"
    ]
    assert notes == []


def test_above_one_lane_the_window_opens_and_records_the_obligation(window):
    """Decision 6: the window may open while candidates are bound to the head,
    provided every such candidate is carried forward and RE-REVIEWED before it
    can be pushed. The note is the record of that obligation — both mergers log
    every note they are handed."""
    set_lanes(window, 2)
    bound_record(window, base="000111222333")
    git = StubWindowGit("000111222333")

    reasons, notes = cli._merge_window_blockers(window, set(), git)

    assert reasons == [], "with a fleet, N-1 lanes are exactly this shape"
    assert len(notes) == 1
    assert "task t-1" in notes[0] and "abc123def456" in notes[0]
    assert "OWES A RE-REVIEW" in notes[0]
    assert "2 lanes" in notes[0]


def test_every_bound_candidate_is_recorded_not_just_the_first(window):
    bound_record(window, task_id="a-1", candidate="aaa111aaa111", base="000111222333")
    bound_record(window, task_id="a-2", candidate="bbb222bbb222", base="000111222333")
    set_lanes(window, 3)

    reasons, notes = cli._merge_window_blockers(window, set(), StubWindowGit("000111222333"))

    assert reasons == []
    assert sorted(n.split(":")[0] for n in notes) == ["task a-1", "task a-2"]
    assert all("OWES A RE-REVIEW" in note for note in notes)


def test_a_base_the_checkout_cannot_place_still_shuts_the_window_above_one_lane(window):
    """The exemption is `BASE_AT_HEAD` and nothing else. A base git cannot place
    is not a base anything can be carried forward FROM, so the obligation could
    not be discharged — "could not tell" must never read as "carry on"."""
    set_lanes(window, 4)
    bound_record(window, base="000111222333")

    reasons, notes = cli._merge_window_blockers(
        window, set(), StubWindowGit("999888777666", place=False)
    )

    assert len(reasons) == 1
    assert "treated as bound to the head" in reasons[0]
    assert notes == []


def test_an_executing_phase_still_shuts_the_window_above_one_lane(window):
    """Untouched by conc-03 and deliberately so: an agent may be mid-write, and
    which lane's phase that is belongs to conc-05/conc-06, not here."""
    set_lanes(window, 2)
    StateStore(window.state_file).save(
        LoopState(session_id="c3", conversation_url=URL, phase=Phase.EXECUTING.value)
    )

    reasons, _notes = cli._merge_window_blockers(window, set(), StubWindowGit("000111222333"))

    assert reasons == ["a phase is executing — an agent may be mid-write"]


def test_a_base_already_behind_keeps_its_own_note_above_one_lane(window):
    """The existing `BASE_BEHIND` exemption is ordered FIRST and keeps its own
    wording: a record that is already behind is not newly owing anything, and
    the two notes must stay distinguishable."""
    set_lanes(window, 2)
    bound_record(window, base="000111222333")

    _reasons, notes = cli._merge_window_blockers(window, set(), StubWindowGit("999888777666"))

    assert len(notes) == 1
    assert "ALREADY behind" in notes[0]
    assert "OWES A RE-REVIEW" not in notes[0]


# =============================================================================
# 3. the carry-forward — the candidate moves, the old approval stops working
# =============================================================================


@pytest.fixture
def repo(tmp_path):
    """The primary checkout a lane's head moves in. Copied from a template
    rather than `init`-ed: six git subprocesses against one `copytree`, which
    CLAUDE.md names as the single most expensive habit in this suite."""
    return make_repo_from_template(
        tmp_path / "repo", branch="work", files=(("f.txt", "one\n"),)
    )


class FakeWorkerRepos:
    """Records what a re-base would have asked for. Nothing here may be called
    on the carry-forward path — that is half of what these tests assert."""

    def __init__(self, root):
        self.root = Path(root)
        self.quarantined: list[str] = []
        self.created: list[str] = []

    def path_for(self, task_id):
        return self.root / task_id

    def quarantine(self, task_id, label):
        self.quarantined.append(label)
        return self.root / f"quarantine/{task_id}-{label}"

    def create(self, task_id, source, base_sha):
        self.created.append(base_sha)

        class _Repo:
            branch = f"autoloop/{task_id}"
            path = self.root / task_id

        return _Repo()


def orchestrator_for(repo, tmp_path, execution, *, lanes=1):
    """An `Orchestrator` carrying only what the stale-base path and
    `_dispatch_task_push` touch — the same hand-built shape
    `test_rebase_stale_base.py` uses, plus a config that names a lane count and
    the registry/state collaborators a push that actually LANDS needs.

    `_config` is a `SimpleNamespace` rather than an `AutoloopConfig` on purpose:
    the only thing the paths under test read from it is the lane count, and a
    stand-in proves that `Orchestrator._lanes` asks for exactly that and nothing
    else. `auto_merge_enabled` is off by default, so a landed push stops at the
    completion rather than reaching the merger.
    """
    from autoloop.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._config = SimpleNamespace(lanes=lanes)
    orch._policy = PolicyEngine(PolicyConfig())
    orch._git = GitGateway(repo, orch._policy)
    orch._worker_repos = FakeWorkerRepos(tmp_path / "workers")
    orch._observed = None
    orch._observed_git = None
    orch._observed_synced_sha = ""
    orch._merge_deferrals = MergeDeferralStore(tmp_path / "deferrals")
    orch._execution_store = TaskExecutionStore(tmp_path / "executions")
    orch._publisher = None
    orch._registry = TaskRegistry([task()])
    orch._registry.mark_in_progress("t1")
    orch._task_store = TaskStore(tmp_path / "tasks.json")
    orch._store = StateStore(tmp_path / "state.json")
    orch.state = LoopState.new(URL)
    orch._logged: list = []
    orch._log = lambda event, **kw: orch._logged.append((event, kw))
    orch._parked: list = []
    orch._to_needs_user = lambda msg, **kw: orch._parked.append((msg, kw))
    orch._execution_store.save(execution)
    return orch


def worker_for(repo, tmp_path, base, *, task_id="t1", path="w.txt", text="worker\n"):
    """A real worker repository holding one committed candidate on the task
    branch — `git init` plus a local fetch, exactly as production builds it."""
    manager = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    worker = manager.create(task_id, repo, base)
    run_git(worker.path, "config", "user.email", "worker@example.com")
    run_git(worker.path, "config", "user.name", "Worker")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    (worker.path / path).write_text(text)
    run_git(worker.path, "add", "-A")
    run_git(worker.path, "commit", "-qm", "the reviewed candidate")
    return worker, head_of(worker.path)


def reviewed(worker, candidate, base, **kw):
    return TaskExecution(
        task_id="t1",
        task_branch=worker.branch,
        worktree_path=str(worker.path),
        task_base_sha=base,
        candidate_sha=candidate,
        **kw,
    )


def move_head(repo, path="mainline.txt", text="a sibling lane shipped\n"):
    (repo / path).write_text(text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", f"mainline: {path}")
    return head_of(repo)


def task():
    return Task(id="t1", title="T", description="d")


def tree_of(repo, sha) -> str:
    return run_git(repo, "rev-parse", f"{sha}^{{tree}}")


def test_at_one_lane_a_carry_forward_leaves_the_candidate_exactly_where_it_was(repo, tmp_path):
    """The base-02 behaviour, unchanged: at one lane the head only moves under a
    reviewed candidate when an operator merges past a shut window, and the
    reviewed sha stays the candidate."""
    old_base = head_of(repo)
    worker, candidate = worker_for(repo, tmp_path, old_base)
    head = move_head(repo)
    execution = reviewed(worker, candidate, old_base, review_round=2)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=1)

    result = orch._rebase_execution_if_stale(execution, task())

    assert result is not None and orch._parked == []
    assert result.task_base_sha == head
    assert result.candidate_sha == candidate
    assert result.review_round == 2
    entry = [kw["data"] for e, kw in orch._logged if e == "execution_base_carried_forward"][0]
    assert entry["rereview_owed"] is False


def test_above_one_lane_a_carry_forward_moves_the_candidate_and_its_tree(repo, tmp_path):
    """THE first half of the claim. The head moved because a sibling lane
    landed, so the reviewed diff now sits on top of work no reviewer of this
    task ever saw — the candidate becomes the merge commit, which is what the
    next review has to be about."""
    old_base = head_of(repo)
    worker, candidate = worker_for(repo, tmp_path, old_base)
    head = move_head(repo)
    execution = reviewed(worker, candidate, old_base, review_round=2, attempt_count=3,
                         fault_attempt_count=1)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=2)

    result = orch._rebase_execution_if_stale(execution, task())

    assert result is not None and orch._parked == [], "the dispatch continues"
    tip = head_of(worker.path)
    assert result.task_base_sha == head
    assert result.candidate_sha == tip != candidate, "the candidate MOVED"
    assert tree_of(worker.path, tip) != tree_of(worker.path, candidate), "and so did its tree"
    assert result.review_round == 0, "and it owes a review"
    # The reviewed object is not rewritten, discarded or unreachable — a merge
    # rewrites nothing, which is why this direction is a merge at all.
    assert run_git(worker.path, "cat-file", "-t", candidate) == "commit"
    assert contains(worker.path, tip, candidate) and contains(worker.path, tip, head)
    # Neither budget is refilled, at any lane count.
    assert result.attempt_count == 3 and result.fault_attempt_count == 1
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []
    # Persisted, and the displaced round is recorded rather than forgotten.
    reloaded = TaskExecutionStore(tmp_path / "executions").load("t1")
    assert reloaded.candidate_sha == tip and reloaded.review_round == 0
    entry = [kw["data"] for e, kw in orch._logged if e == "execution_base_carried_forward"][0]
    assert entry["rereview_owed"] is True
    assert entry["candidate_sha"] == candidate and entry["new_candidate_sha"] == tip
    assert entry["review_round_before"] == 2


def test_the_old_postcommit_binding_is_refused_after_a_carry_forward(repo, tmp_path):
    """The second half: an approval taken against the pre-merge candidate can no
    longer publish anything, and it is the EXISTING push-time check that refuses
    it — the record and the binding simply no longer name the same commit."""
    old_base = head_of(repo)
    worker, candidate = worker_for(repo, tmp_path, old_base)
    move_head(repo)
    execution = reviewed(worker, candidate, old_base, review_round=1)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=2)
    binding = PostcommitBinding(
        task_id="t1",
        task_branch=worker.branch,
        base_sha=old_base,
        candidate_sha=candidate,
        candidate_tree_sha=tree_of(worker.path, candidate),
        packet_sha256="0" * 64,
    )

    orch._rebase_execution_if_stale(execution, task())
    orch._dispatch_task_push(
        Directive(decision=Decision.PUSH, reason="approved"),
        LastResponse(request_id="r1", raw="{}", received_at="now", postcommit=binding),
        binding,
    )

    assert len(orch._parked) == 1
    message, kw = orch._parked[0]
    assert kw["code"] == "push_candidate_stale"
    assert "no longer this task's current candidate" in message


def test_a_carried_forward_candidate_is_carried_forward_AGAIN_not_quarantined(repo, tmp_path):
    """The reset's own hazard, pinned. `review_round` is 0 after the first
    carry-forward, so a branch selection keyed on that counter would send the
    SECOND one down the re-base arm and quarantine the worker holding the first
    merge — discarding reviewed work through the fix for discarding reviewed
    work. With four lanes the head moves again constantly, so this is the
    ordinary case rather than an exotic one."""
    old_base = head_of(repo)
    worker, candidate = worker_for(repo, tmp_path, old_base)
    move_head(repo)
    execution = reviewed(worker, candidate, old_base, review_round=1)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=2)

    orch._rebase_execution_if_stale(execution, task())
    first_merge = head_of(worker.path)
    second_head = move_head(repo, path="mainline2.txt", text="another lane shipped\n")

    result = orch._rebase_execution_if_stale(execution, task())

    assert result is not None and orch._parked == []
    assert orch._worker_repos.quarantined == [], "the worker holding the merge survives"
    assert orch._worker_repos.created == []
    tip = head_of(worker.path)
    assert result.task_base_sha == second_head
    assert result.candidate_sha == tip != first_merge
    assert contains(worker.path, tip, candidate), "the reviewed object is STILL reachable"
    assert contains(worker.path, tip, second_head)
    assert (worker.path / "w.txt").read_text() == "worker\n", "the task's work is untouched"


def test_a_conflicting_carry_forward_above_one_lane_parks_and_changes_nothing(repo, tmp_path):
    """The park stays the park, at every lane count. Resolving a conflict on a
    reviewed candidate's behalf is the quiet discard this path exists to refuse,
    and a fleet is not a reason to start."""
    old_base = head_of(repo)
    worker, candidate = worker_for(repo, tmp_path, old_base, path="f.txt", text="task's line\n")
    head = move_head(repo, path="f.txt", text="the sibling lane's line\n")
    execution = reviewed(worker, candidate, old_base, review_round=1, attempt_count=2)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=2)

    result = orch._rebase_execution_if_stale(execution, task())

    assert result is None
    message, kw = orch._parked[0]
    assert kw["code"] == "task_base_behind_head"
    assert "conflicts at f.txt" in message
    assert head[:12] in kw["detail"] and old_base[:12] in kw["detail"]
    # The record is INTACT: nothing re-pointed, no candidate moved, no round
    # reset — a refused carry-forward must leave the round exactly as it found
    # it, or the park would cost the very work it protects.
    stored = TaskExecutionStore(tmp_path / "executions").load("t1")
    assert stored.task_base_sha == old_base and stored.candidate_sha == candidate
    assert stored.review_round == 1 and stored.attempt_count == 2
    # And so is the worker repository.
    assert head_of(worker.path) == candidate
    assert run_git(worker.path, "status", "--porcelain") == ""
    assert (worker.path / "f.txt").read_text() == "task's line\n"
    assert orch._worker_repos.quarantined == []


def test_a_park_for_an_unreviewed_candidate_does_not_claim_a_review_ran(repo, tmp_path):
    """Above one lane the carry-forward arm also takes records whose round was
    reset (and records that never reached a review at all), so the park's text
    has to stop asserting that a review had run. The reviewed wording is
    unchanged — `test_rebase_stale_base.py` pins it."""
    old_base = head_of(repo)
    # A record naming a worker directory that does not exist: the carry-forward
    # has nothing to merge into and refuses, which is the residual park.
    execution = TaskExecution(
        task_id="t1", task_branch="autoloop/t1", worktree_path=str(repo / "gone"),
        task_base_sha=old_base, candidate_sha="c" * 40, review_round=0,
    )
    move_head(repo)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=2)

    result = orch._rebase_execution_if_stale(execution, task())

    assert result is None
    message, kw = orch._parked[0]
    assert kw["code"] == "task_base_behind_head"
    assert "owes a re-review" in message
    assert "a review round has already run" not in message
    assert orch._worker_repos.quarantined == [], "nothing was rebuilt over it either"


def test_at_one_lane_an_unreviewed_candidate_still_rebases_and_rebuilds(repo, tmp_path):
    """The other side of the widened predicate: at one lane a record with a
    committed-but-never-reviewed candidate takes the re-base arm exactly as it
    always has — worker quarantined, candidate cleared, budget preserved."""
    old_base = head_of(repo)
    execution = TaskExecution(
        task_id="t1", task_branch="autoloop/t1", worktree_path=str(repo / "gone"),
        task_base_sha=old_base, candidate_sha="c" * 40, review_round=0, attempt_count=2,
    )
    head = move_head(repo)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=1)

    result = orch._rebase_execution_if_stale(execution, task())

    assert result is not None and orch._parked == []
    assert result.task_base_sha == head and result.candidate_sha == ""
    assert orch._worker_repos.created == [head] and orch._worker_repos.quarantined


# =============================================================================
# 4. the push guard — the ordering no carry-forward has run in
# =============================================================================


def test_above_one_lane_an_approval_whose_base_moved_is_refused(repo, tmp_path):
    """THE fail-open this candidate would otherwise have. A sibling lane's merge
    lands while this task is AWAITING: no dispatch has run, so nothing carried
    anything forward, the record still names the reviewed sha, its tree still
    matches, and every existing check agrees. Publishing there is exactly
    Decision 3's step 3 — an approval taken against the pre-merge base."""
    old_base = head_of(repo)
    worker, candidate = worker_for(repo, tmp_path, old_base)
    move_head(repo)
    execution = reviewed(worker, candidate, old_base, review_round=1)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=2)
    binding = PostcommitBinding(
        task_id="t1", task_branch=worker.branch, base_sha=old_base,
        candidate_sha=candidate, candidate_tree_sha=tree_of(worker.path, candidate),
        packet_sha256="0" * 64,
    )

    orch._dispatch_task_push(
        Directive(decision=Decision.PUSH, reason="approved"),
        LastResponse(request_id="r1", raw="{}", received_at="now", postcommit=binding),
        binding,
    )

    assert len(orch._parked) == 1
    message, kw = orch._parked[0]
    assert kw["code"] == "push_base_moved", (
        "its own code: `push_candidate_stale` means the RECORD moved on, and its "
        "single park site is pinned structurally next door"
    )
    assert "the base this approval was taken against has moved" in message
    assert "Nothing was pushed" in message


def test_an_unplaceable_base_refuses_the_push_too(repo, tmp_path):
    """Fail closed on the same rule the merge window applies: "could not tell"
    is never "safe to publish"."""
    worker, candidate = worker_for(repo, tmp_path, head_of(repo))
    execution = reviewed(worker, candidate, "b" * 40, review_round=1)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=2)
    binding = PostcommitBinding(
        task_id="t1", task_branch=worker.branch, base_sha="b" * 40,
        candidate_sha=candidate, candidate_tree_sha=tree_of(worker.path, candidate),
        packet_sha256="0" * 64,
    )

    orch._dispatch_task_push(
        Directive(decision=Decision.PUSH, reason="approved"),
        LastResponse(request_id="r1", raw="{}", received_at="now", postcommit=binding),
        binding,
    )

    assert orch._parked and orch._parked[0][1]["code"] == "push_base_moved"


def test_at_one_lane_the_same_moved_base_pushes_exactly_as_it_does_today(repo, tmp_path):
    """The byte-identical half. An operator merging past the window at one lane
    moves the head under an approved candidate, and the push still lands: the
    guard above evaluates nothing at one lane, and this is what proves it."""
    old_base = head_of(repo)
    worker, candidate = worker_for(repo, tmp_path, old_base)
    move_head(repo)
    execution = reviewed(worker, candidate, old_base, review_round=1)
    orch = orchestrator_for(repo, tmp_path, execution, lanes=1)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    run_git(worker.path, "remote", "add", "origin", str(origin))
    execution.intended_remote = "origin"
    execution.intended_remote_ref = f"refs/heads/{worker.branch}"
    orch._execution_store.save(execution)
    binding = PostcommitBinding(
        task_id="t1", task_branch=worker.branch, base_sha=old_base,
        candidate_sha=candidate, candidate_tree_sha=tree_of(worker.path, candidate),
        packet_sha256="0" * 64,
    )

    orch._dispatch_task_push(
        Directive(decision=Decision.PUSH, reason="approved"),
        LastResponse(request_id="r1", raw="{}", received_at="now", postcommit=binding),
        binding,
    )

    assert orch._parked == [], "one lane refuses nothing new"
    worker_git = GitGateway(worker.path, PolicyEngine(PolicyConfig()))
    assert worker_git.remote_ref_sha("origin", f"refs/heads/{worker.branch}") == candidate


# =============================================================================
# 5. the sweep — re-evaluated between merges, all-or-nothing unchanged
# =============================================================================


class Backlog:
    """A checkout, its origin, and however many published branches a test asks
    for — the same shape `test_merge_sweep.py` builds, trimmed to what these
    tests need."""

    def __init__(self, repo, origin, config):
        self.repo = repo
        self.origin = origin
        self.config = config
        self.execution_store = TaskExecutionStore(config.executions_dir)
        self.task_store = TaskStore(config.tasks_file)
        self.registry = TaskRegistry()
        self.task_store.save(self.registry)

    def head(self):
        return head_of(self.repo)

    def publish(self, task_id, files):
        base_sha = self.head()
        branch = f"autoloop/{task_id}"
        ref = f"refs/heads/{branch}"
        run_git(self.repo, "checkout", "-q", "-b", branch, base_sha)
        for rel, content in files.items():
            (self.repo / rel).write_text(content, encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-qm", f"work for {task_id}")
        sha = self.head()
        run_git(self.repo, "push", "-q", "origin", f"{sha}:{ref}")
        run_git(self.repo, "checkout", "-q", BASE)
        self.execution_store.save(TaskExecution(
            task_id=task_id, task_branch=branch, worktree_path="",
            task_base_sha=base_sha, candidate_sha=sha, review_round=1,
            intended_remote="origin", intended_remote_ref=ref, published_sha=sha,
        ))
        self.registry.add_many([Task(id=task_id, title=f"Title {task_id}", description="d")])
        self.registry.mark_completed(task_id)
        self.task_store.save(self.registry)
        return sha

    def in_flight(self, task_id, base_sha):
        """An UNPUBLISHED candidate bound to the base — the record that holds
        the window shut at one lane and owes a re-review above one."""
        self.execution_store.save(TaskExecution(
            task_id=task_id, task_branch=f"autoloop/{task_id}", worktree_path="",
            task_base_sha=base_sha, candidate_sha="c" * 40, review_round=1,
        ))
        self.registry.add_many([Task(id=task_id, title="in flight", description="d")])
        self.task_store.save(self.registry)

    def sweep(self):
        return merge_sweep.sweep_backlog(
            self.config, git=GitGateway(self.repo, PolicyEngine(self.config.policy))
        )

    def entries(self, entry_type):
        path = self.config.transcript_file
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if r["type"] == entry_type]


@pytest.fixture
def backlog(tmp_path):
    repo = make_repo_from_template(tmp_path / "checkout", branch=BASE)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "-u", "origin", BASE)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(auto_merge_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers",
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return Backlog(repo, origin, config)


def test_at_one_lane_a_bound_candidate_defers_the_whole_backlog(backlog):
    """Today's sweep, unchanged: the gate is shut, so NOTHING is attempted and
    the base is exactly where it was."""
    before = backlog.head()
    for task_id in ("a1", "a2", "a3"):
        backlog.publish(task_id, {f"{task_id}.py": "work\n"})
    backlog.in_flight("held", backlog.head())

    result = backlog.sweep()

    assert result.outcome == merge_sweep.DEFERRED
    assert result.merged == []
    assert backlog.head() == before
    assert backlog.entries("auto_merge_pushed") == []


def test_above_one_lane_three_branches_land_and_the_obligation_is_re_evaluated(backlog):
    """Decision 6's serialisation half. Each merge moves the base for every
    candidate that is not it, so the obligation is a different fact after every
    merge — and because the gate is re-derived per branch, the transcript
    carries one verdict per branch rather than one for the sweep: bound to the
    head before the first merge, already behind after it."""
    set_lanes(backlog.config, 2)
    for task_id in ("a1", "a2", "a3"):
        backlog.publish(task_id, {f"{task_id}.py": "work\n"})
    base_before = backlog.head()
    backlog.in_flight("held", base_before)

    result = backlog.sweep()

    assert result.outcome == merge_sweep.SWEPT
    assert sorted(result.merged) == ["a1", "a2", "a3"]
    assert backlog.head() != base_before
    assert [e["data"]["task_id"] for e in backlog.entries("auto_merge_pushed")] == result.merged
    # The pre-sweep gate saw a candidate bound to the head.
    pre = [e["data"]["note"] for e in backlog.entries("merge_sweep_window_note")]
    assert len(pre) == 1 and "OWES A RE-REVIEW" in pre[0]
    # And the per-branch gate re-derived it three times, from git, between the
    # merges — the first before anything moved, the other two after.
    per_branch = [e["data"]["note"] for e in backlog.entries("auto_merge_window_note")]
    assert len(per_branch) == 3, "once per branch, not once for the sweep"
    assert "OWES A RE-REVIEW" in per_branch[0]
    assert all("ALREADY behind" in note for note in per_branch[1:]), (
        "after the first merge the same record is behind the head, not bound to it"
    )
    # The record was never touched by any of it: the obligation is discharged by
    # the task's own next dispatch, not by the merger.
    held = backlog.execution_store.load("held")
    assert held.task_base_sha == base_before and held.review_round == 1


def test_above_one_lane_a_genuine_blocker_still_defers_the_whole_sweep(backlog):
    """The all-or-nothing property, unchanged. Only `BASE_AT_HEAD` was exempted:
    a base git cannot place still shuts the gate at every lane count, and a shut
    gate still means nothing is attempted at all."""
    set_lanes(backlog.config, 3)
    before = backlog.head()
    for task_id in ("a1", "a2", "a3"):
        backlog.publish(task_id, {f"{task_id}.py": "work\n"})
    backlog.in_flight("unplaceable", "b" * 40)

    result = backlog.sweep()

    assert result.outcome == merge_sweep.DEFERRED
    assert result.merged == []
    assert sorted(result.pending) == ["a1", "a2", "a3"]
    assert backlog.head() == before, "not one branch was attempted"
    assert backlog.entries("auto_merge_pushed") == []


def test_above_one_lane_an_executing_phase_still_defers_the_whole_sweep(backlog):
    set_lanes(backlog.config, 2)
    before = backlog.head()
    backlog.publish("a1", {"a1.py": "work\n"})
    StateStore(backlog.config.state_file).save(
        LoopState(session_id="c3", conversation_url=URL, phase=Phase.EXECUTING.value)
    )

    result = backlog.sweep()

    assert result.outcome == merge_sweep.DEFERRED
    assert backlog.head() == before


def test_the_sweep_still_stops_at_the_first_branch_that_does_not_land(backlog):
    """Stopping is orthogonal to the lane count and stays that way: a conflict
    aborts, the sweep halts, and the branches behind it are left untouched and
    named."""
    set_lanes(backlog.config, 2)
    backlog.publish("a1", {"shared.py": "one lane's line\n"})
    backlog.publish("a2", {"shared.py": "another lane's line\n"})
    # The base moves under both, so the second cannot merge cleanly.
    (backlog.repo / "shared.py").write_text("the base's own line\n")
    run_git(backlog.repo, "add", "-A")
    run_git(backlog.repo, "commit", "-qm", "the base takes that file")
    before = backlog.head()

    result = backlog.sweep()

    assert result.outcome == merge_sweep.STOPPED
    assert result.stopped_on == "a1"
    assert result.stopped_outcome == auto_merge.CONFLICT
    assert result.pending == ["a1", "a2"], "the rest are named, not attempted"
    assert backlog.head() == before, "and the base is exactly where it was"
    assert result.is_reconciled, "the conflict aborted back to the pre-merge head"
    assert run_git(backlog.repo, "status", "--porcelain") == "", "no merge residue"
