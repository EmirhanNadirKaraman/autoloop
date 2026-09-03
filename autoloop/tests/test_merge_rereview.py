"""A candidate whose base moved is carried forward and RE-REVIEWED — never
merged or pushed on the approval it already had (conc-03, docs/AUTOLOOP.md
"Decision 6 — merging is serialised and rebase-aware").

The measured problem this is about: `cli._merge_window_blockers` shuts the
window whenever ANY execution record holds a candidate bound to the current
head. That is a fleet-wide mutual exclusion — with N lanes, N−1 of them are
exactly that record — so under concurrency the window would essentially never
open. Decision 6 replaces the blanket block with a per-candidate OBLIGATION,
and everything below is about what makes that safe.

Two halves, and the split is deliberate. The window predicate is a pure
function of records on disk plus one gateway, so it is tested from records a
test writes directly — the plan says so itself ("it needs two execution
*records*, which a test writes directly, not two live agents"). The
carry-forward is a claim about GIT, so it gets real repositories, real
worktrees, a real remote and the real orchestrator.

The third claim, and the one the carry-forward would be useless without: a
carried candidate does not wait for a human to notice it. The lane holding the
now-invalid approval sends a FRESH packet for it and can publish only the
binding that packet produces — asserted end to end, through `_step_ready`,
rather than by writing the record a discharge would have left. Five states in
which the loop must NOT ask (a carry that refused, an outstanding stat-only
split ask, a spent round budget, a task the registry lost, a worker that cannot
be read) each keep the park, with the reason they did not ask in the transcript.

`lanes = 1` is the acceptance criterion every candidate in that plan carries,
and it is asserted here rather than assumed: the reason string, the merge
outcome and the untouched record are pinned at one lane in the same file that
exercises two, so a change that moved the single-lane path fails here rather
than in some existing test nobody meant to edit.

Real git throughout the second half, self-contained helpers, matching this
package's convention (see `test_auto_merge.py`, whose `build`/`Harness` this
mirrors — duplicated rather than imported, like every other suite here).
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

from autoloop import auto_merge, cli, merge_sweep
from autoloop.auto_merge import MergeObligation
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, ConcurrencyConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import GitCommandError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    LastResponse,
    LoopState,
    Phase,
    PostcommitBinding,
    StateStore,
)
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import (
    ATTEMPT_TASK,
    IntentStore,
    TaskExecutionStore,
    format_attempt,
)
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/conc-03"
BASE = "work"
BASE_REF = f"refs/heads/{BASE}"


# --- the window predicate ------------------------------------------------------
#
# Records on disk plus one stub gateway. No repository, no subprocess: the
# CLAIM here is about a pure decision over records, and a real repo would buy
# nothing but seconds.


class _Placer:
    """Stands in for the window's gateway. Names a head and places a base
    against it — the two questions `_candidate_base_ancestry` asks — and
    records every remote lookup so "it never went to the network" stays a
    checkable claim."""

    def __init__(self, head="h" * 40, behind=(), fail=False):
        self.head = head
        self.behind = set(behind)
        self.fail = fail
        self.lookups = []

    def head_sha(self):
        return self.head

    def is_descendant(self, head, base):
        if self.fail:
            raise GitCommandError("merge-base", f"{base}: not a valid object name")
        return base in self.behind

    def remote_ref_sha(self, remote, dest_ref):
        self.lookups.append((remote, dest_ref))
        return ""

    def read_commit(self, sha):
        # A checkout that HOLDS every candidate it is asked about, which is what
        # keeps `_candidate_is_retired` answering "" here: these records are
        # in-flight work, not the released-and-quarantined shape that predicate
        # is about, and a gateway that could not resolve them would exempt them
        # for a reason none of these tests is making a claim about.
        return {"tree": "t" * 40, "parents": (), "subject": "work"}


def window_config(tmp_path, *, lanes=1):
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".autoloop",
        workers_root=tmp_path / "workers",
        concurrency=ConcurrencyConfig(lanes=lanes),
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def write_record(config, task_id, *, base, candidate="c" * 40, **extra):
    directory = config.executions_dir
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "task_branch": f"autoloop/{task_id}",
        "worktree_path": "",
        "task_base_sha": base,
        "candidate_sha": candidate,
        "review_round": 1,
    }
    payload.update(extra)
    (directory / f"{task_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def register(config, *task_ids):
    TaskStore(config.tasks_file).save(
        TaskRegistry([
            Task(id=tid, title=f"Title {tid}", description="d") for tid in task_ids
        ])
    )


def test_at_one_lane_a_bound_candidate_shuts_the_window_in_todays_words(tmp_path):
    """THE acceptance criterion, pinned as a literal. Not "a reason mentioning
    the task" — the whole sentence, because the plan's promise is that the
    single-lane predicate is byte-identical and a reworded refusal is exactly
    what that forbids."""
    config = window_config(tmp_path, lanes=1)
    register(config, "t9")
    placer = _Placer()
    write_record(config, "t9", base=placer.head)

    obligations: list = []
    reasons, notes = cli._merge_window_blockers(
        config, set(), placer, obligations=obligations
    )

    assert reasons == [
        "task t9 has a candidate (cccccccccccc) bound to base hhhhhhhhhhhh — "
        "never pushed; that base IS the current head hhhhhhhhhhhh, so merging "
        "would strand it"
    ]
    assert notes == []
    assert obligations == [], "one lane owes nothing: the record BLOCKS"


def test_above_one_lane_the_window_opens_and_the_candidate_owes_a_re_review(tmp_path):
    """The Decision 6 substitution. The record stops holding the window and
    starts holding a DEBT, reported both ways: a note an operator reads and a
    `MergeObligation` the merger discharges."""
    config = window_config(tmp_path, lanes=2)
    register(config, "t9")
    placer = _Placer()
    (tmp_path / "w9").mkdir()
    write_record(config, "t9", base=placer.head, worktree_path=str(tmp_path / "w9"))

    obligations: list = []
    reasons, notes = cli._merge_window_blockers(
        config, set(), placer, obligations=obligations
    )

    assert reasons == [], "the window OPENS at two lanes"
    assert obligations == [
        MergeObligation(
            task_id="t9",
            candidate_sha="c" * 40,
            base_sha=placer.head,
            worktree_path=str(tmp_path / "w9"),
        )
    ]
    assert len(notes) == 1
    assert "OWES A RE-REVIEW" in notes[0]
    assert "t9" in notes[0] and "2 lanes" in notes[0]


def test_every_bound_candidate_is_recorded_not_just_the_first(tmp_path):
    """N−1 lanes hold one each. A predicate that reported the first and
    returned would leave the rest bound to a head about to move with nothing
    recorded against them — which is the whole failure, one record along."""
    config = window_config(tmp_path, lanes=3)
    register(config, "t7", "t8", "t9")
    placer = _Placer()
    for task_id in ("t7", "t8", "t9"):
        write_record(config, task_id, base=placer.head, candidate=task_id * 8)

    obligations: list = []
    reasons, _notes = cli._merge_window_blockers(
        config, set(), placer, obligations=obligations
    )

    assert reasons == []
    assert sorted(o.task_id for o in obligations) == ["t7", "t8", "t9"]


def test_a_base_git_cannot_place_still_shuts_the_window_at_every_lane_count(tmp_path):
    """FAIL CLOSED, unchanged. "Cannot be shown to be bound to the head" is not
    "is bound to it and can be carried past it" — there is nothing to carry a
    candidate onto when git will not say where its base sits, so the
    obligation arm is deliberately `BASE_AT_HEAD` only."""
    config = window_config(tmp_path, lanes=4)
    register(config, "t9")
    placer = _Placer(fail=True)
    write_record(config, "t9", base="0" * 40)

    obligations: list = []
    reasons, _notes = cli._merge_window_blockers(
        config, set(), placer, obligations=obligations
    )

    assert len(reasons) == 1
    assert "treated as bound to the head" in reasons[0]
    assert obligations == [], "an unplaceable base is never an obligation"


def test_a_base_already_behind_stays_the_note_it_has_always_been(tmp_path):
    """The exemptions run FIRST and keep their meanings. A record the head is
    already past is not in-flight work the obligation machinery should mark,
    mark-and-fail on, or park — it is the note this predicate has produced
    since 2026-08-21."""
    config = window_config(tmp_path, lanes=2)
    register(config, "t9")
    placer = _Placer(behind={"0" * 40})
    write_record(config, "t9", base="0" * 40)

    obligations: list = []
    reasons, notes = cli._merge_window_blockers(
        config, set(), placer, obligations=obligations
    )

    assert reasons == []
    assert obligations == []
    assert "ALREADY behind" in notes[0]


def test_a_terminal_task_produces_no_obligation(tmp_path):
    """A completed task's record is exempt before the ancestry question is
    even asked, at two lanes exactly as at one. Marking one would demand a
    re-review of work that has shipped."""
    config = window_config(tmp_path, lanes=2)
    registry = TaskRegistry([Task(id="t9", title="t", description="d")])
    registry.mark_completed("t9")
    TaskStore(config.tasks_file).save(registry)
    placer = _Placer()
    write_record(config, "t9", base=placer.head)

    obligations: list = []
    reasons, _notes = cli._merge_window_blockers(
        config, set(), placer, obligations=obligations
    )

    assert (reasons, obligations) == ([], [])


def test_an_executing_phase_still_shuts_the_window_at_two_lanes(tmp_path):
    """Untouched, and named so the narrowing is on the record: a lane mid-write
    is not a candidate obligation, has no carry-forward to owe, and still
    stops a merge."""
    config = window_config(tmp_path, lanes=2)
    register(config, "t9")
    placer = _Placer()
    write_record(config, "t9", base=placer.head)
    StateStore(config.state_file).save(
        LoopState(session_id="s", conversation_url=URL, phase=Phase.EXECUTING.value)
    )

    reasons, _notes = cli._merge_window_blockers(config, set(), placer)

    assert reasons == ["a phase is executing — an agent may be mid-write"]


def test_a_caller_that_passes_no_list_still_gets_the_open_window(tmp_path):
    """`merge-window` and the sweep's own gate call pass nothing. The verdict
    must not depend on whether anyone wanted the obligations — a predicate that
    answered differently for its two callers is the drift this module's own
    docstring exists to prevent."""
    config = window_config(tmp_path, lanes=2)
    register(config, "t9")
    placer = _Placer()
    write_record(config, "t9", base=placer.head)

    reasons, notes = cli._merge_window_blockers(config, set(), placer)

    assert reasons == []
    assert "OWES A RE-REVIEW" in notes[0]


# --- the carry-forward ---------------------------------------------------------
#
# Real repositories from here down. The claim is about git: which commit a
# candidate becomes, whether an abort restored a worktree, what a push refuses.


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def head(repo) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def tree_of(repo, sha) -> str:
    return run_git(repo, "rev-parse", f"{sha}^{{tree}}").strip()


def ref_sha(repo, ref) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", ref], cwd=str(repo), capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def contains(repo, descendant, ancestor) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo), capture_output=True, text=True,
    ).returncode == 0


def is_clean(repo) -> bool:
    return not run_git(repo, "status", "--porcelain").strip()


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class WritingExecutor:
    def __init__(self, worktrees_root, per_task):
        self.worktrees_root = Path(worktrees_root)
        self.per_task = {k: dict(v) for k, v in per_task.items()}

    def execute(self, directive, task):
        files = self.per_task[task.id]
        wt = self.worktrees_root / task.id
        for rel, content in files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary=f"wrote {sorted(files)}",
            details="details",
            validation="ok",
            changed_paths=tuple(files.keys()),
        )


class Harness:
    def __init__(self, orch, repo, origin, config, execution_store, tasks):
        self.orch = orch
        self.repo = repo
        self.origin = origin
        self.config = config
        self.execution_store = execution_store
        self.tasks = tasks

    def stage(self, task_id):
        self.orch._dispatch_executor(
            Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)
        )
        self.orch._step_ready()
        req = self.orch.state.pending_request
        return LastResponse(
            request_id=req.request_id, raw="{}", received_at="now",
            head_sha=req.head_sha, base_sha=req.base_sha,
            report_sha256=req.report_sha256, postcommit=req.postcommit,
        )

    def push(self, task_id):
        self.orch._dispatch_task_push(
            Directive(decision=Decision.PUSH, reason="approved"), self.stage(task_id)
        )
        return self.execution_store.load(task_id)

    def entries(self, entry_type=None):
        path = self.config.transcript_file
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if entry_type is None or r["type"] == entry_type]

    def head(self):
        return head(self.repo)

    def origin_base(self):
        return ref_sha(self.origin, BASE_REF)

    def blockers(self, task_id):
        return [
            b for b in BlockerStore(self.config.blockers_dir).all_blockers()
            if b.task_id == task_id
        ]


def build(
    tmp_path, *, per_task, lanes=2, auto_merge_enabled=True, max_review_rounds=0
):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", BASE)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "-u", "origin", BASE)

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(
            implement_enabled=True,
            auto_merge_enabled=auto_merge_enabled,
            max_review_rounds=max_review_rounds,
        ),
        state_dir=tmp_path / ".al",
        concurrency=ConcurrencyConfig(lanes=lanes),
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    git = GitGateway(repo, PolicyEngine(config.policy))
    worktrees = WorktreeManager(git, tmp_path / "worktrees")
    executor = WritingExecutor(tmp_path / "worktrees", per_task)
    execution_store = TaskExecutionStore(config.executions_dir)

    tasks = [
        Task(id=tid, title=f"Title {tid}", description="desc",
             approved_paths=tuple(sorted(files)))
        for tid, files in per_task.items()
    ]
    registry = TaskRegistry(tasks)
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    def no_client():
        raise AssertionError("no browser client expected in this test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worktrees=worktrees,
        execution_store=execution_store,
        intent_store=IntentStore(config.intents_dir),
        validation_runner=ok_validation,
        # Wired, unlike `test_auto_merge.py`'s otherwise-identical harness: the
        # park is half of what several tests here assert, and `_to_needs_user`
        # records nothing without a store. `auto_merge` builds its own from
        # `config.blockers_dir`, so both writers land in the same directory.
        blocker_store=BlockerStore(config.blockers_dir),
    )
    return Harness(orch, repo, origin, config, execution_store, {t.id: t for t in tasks})


def bound_candidate(h, task_id, rel, content, *, review_round=1):
    """A REVIEWED candidate on its own branch, in its own worktree, bound to
    the current head — built by hand rather than through `stage` so a test can
    hold one open while ANOTHER task runs a full round. That is the shape a
    second lane produces, and it is the only shape this whole feature is about.
    """
    h.orch._registry.add_many(
        [Task(id=task_id, title=task_id, description="d", approved_paths=(rel,))]
    )
    h.orch._task_store.save(h.orch._registry)
    execution = h.orch._worktrees.create(task_id, h.head())
    worktree = Path(execution.worktree_path)
    (worktree / rel).write_text(content, encoding="utf-8")
    run_git(worktree, "add", "-A")
    run_git(worktree, "commit", "-q", "-m", f"{task_id}: work")
    execution.candidate_sha = head(worktree)
    execution.candidate_commit_count = 1
    execution.review_round = review_round
    h.execution_store.save(execution)
    return execution


def binding_for(execution, repo_for_tree) -> PostcommitBinding:
    """The approval a reviewer would have given for `execution`'s candidate,
    exactly as `_finish_postcommit` captures it."""
    return PostcommitBinding(
        task_id=execution.task_id,
        task_branch=execution.task_branch,
        base_sha=execution.task_base_sha,
        candidate_sha=execution.candidate_sha,
        candidate_tree_sha=tree_of(repo_for_tree, execution.candidate_sha),
        packet_sha256="p" * 64,
    )


def approve(h, binding):
    h.orch._dispatch_task_push(
        Directive(decision=Decision.PUSH, reason="approved"),
        LastResponse(request_id="r", raw="{}", received_at="now"),
        binding,
    )


def queued_review_packet(h) -> str:
    """The produce-then-review packet waiting to be sent, or `""`.

    Not `state.outbox is None`: the push that moved the head leaves its own
    `git_report` there, so "no packet was queued" and "the outbox is empty" are
    different claims and only the first one is being made anywhere below."""
    outbox = h.orch.state.outbox or ""
    return outbox if "POST-COMMIT REVIEW PACKET" in outbox else ""


def test_a_merge_at_two_lanes_carries_the_bound_candidate_onto_the_new_head(tmp_path):
    """The claim, in one test. Another task merges, the head moves, and the
    reviewed candidate is not stranded — it is carried onto the new head, its
    sha and its tree both move, and the rounds it had are preserved rather than
    forgotten."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2)
    before = h.head()
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    worktree = Path(nine.worktree_path)
    old_tree = tree_of(worktree, nine.candidate_sha)

    h.push("t1")

    after = h.head()
    assert after != before, "the base moved: this is the situation, not a bug"
    carried = h.execution_store.load("t9")
    assert carried.task_base_sha == after
    assert carried.candidate_sha != nine.candidate_sha, "the candidate sha moved"
    assert tree_of(worktree, carried.candidate_sha) != old_tree, "and so did the tree"
    assert contains(worktree, carried.candidate_sha, nine.candidate_sha), (
        "a MERGE, not a re-base: the reviewed commit still exists and is reachable"
    )
    assert contains(worktree, carried.candidate_sha, after)
    assert carried.review_round == 0, "reset, so the loop asks for the new review"
    assert carried.carried_review_rounds == 1, "and the round it had is not refilled"
    assert carried.rereview_owed_base == before
    assert [e["data"]["task_id"] for e in h.entries("auto_merge_rereview_owed")] == ["t9"]
    assert [
        e["data"]["task_id"] for e in h.entries("auto_merge_candidate_carried_forward")
    ] == ["t9"]


def test_the_old_approval_is_refused_after_the_carry_forward(tmp_path):
    """"Never pushed on its old approval." The reviewer approved a candidate
    against a base that has since moved; the binding still names it, and
    `_dispatch_task_push` publishes nothing.

    What it does INSTEAD of parking is the other half of Decision 6 — the round
    was reset "so the loop asks for the new review instead of parking" — so the
    refusal is asserted here together with the ask that replaces the park."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2)
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    stale = binding_for(nine, Path(nine.worktree_path))

    h.push("t1")
    approve(h, stale)

    # REFUSED, which is the graded sentence: nothing reached the remote, the
    # record records no publication, and the task did not complete.
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == "", (
        "nothing was published on the stale approval"
    )
    assert h.execution_store.load("t9").published_sha == ""
    assert h.orch._registry.state_of("t9") is not TaskState.COMPLETED
    # ASKED, not parked: no blocker for t9, and a packet naming the carried
    # candidate is queued for the reviewer.
    assert h.blockers("t9") == []
    assert h.orch.state.phase == Phase.READY.value
    carried = h.execution_store.load("t9")
    assert carried.candidate_sha in queued_review_packet(h)


def test_a_carry_forward_that_conflicts_parks_and_destroys_nothing(tmp_path):
    """The refusal path. Both branches add the same file, so the head cannot be
    merged into the task branch — and the whole point of the park is that it
    costs a human's attention, never the work."""
    h = build(tmp_path, per_task={"t1": {"shared.py": "one\n"}}, lanes=2)
    before = h.head()
    nine = bound_candidate(h, "t9", "shared.py", "nine\n")
    worktree = Path(nine.worktree_path)
    tip_before = head(worktree)

    h.push("t1")

    assert h.head() != before, "the merge that moved the head still happened"
    parks = [b for b in h.blockers("t9") if b.code == "task_base_behind_head"]
    assert len(parks) == 1
    assert "shared.py" in parks[0].question or "conflicts" in parks[0].question
    # The worker repository, intact.
    assert is_clean(worktree), "the carry-forward's own merge was aborted"
    assert head(worktree) == tip_before
    assert run_git(worktree, "branch", "--show-current").strip() == nine.task_branch
    # The record, intact.
    kept = h.execution_store.load("t9")
    assert kept.candidate_sha == nine.candidate_sha
    assert kept.task_base_sha == before
    assert kept.review_round == 1
    assert kept.carried_review_rounds == 0


def test_a_failed_carry_forward_still_refuses_the_old_approval(tmp_path):
    """THE fail-open this design is arranged around, and the reason the marker
    is written BEFORE the merge rather than derived from the carry afterwards.

    The carry-forward refused, so `candidate_sha` and its tree are untouched
    and every push-time check would pass. Only the marker stands between an
    approval taken against a base that has since moved and a publish.

    It is also the one owed re-review the loop must NOT ask for: no carried
    candidate exists, the record is still on a base the head has moved past,
    and asking a reviewer to look again at a commit a human has to unstick
    would spend a round on the wrong question. So this one PARKS, exactly as it
    always did, and says why it did not ask."""
    h = build(tmp_path, per_task={"t1": {"shared.py": "one\n"}}, lanes=2)
    before = h.head()
    nine = bound_candidate(h, "t9", "shared.py", "nine\n")
    stale = binding_for(nine, Path(nine.worktree_path))

    h.push("t1")

    kept = h.execution_store.load("t9")
    assert kept.candidate_sha == stale.candidate_sha, (
        "the precondition of this test: every OTHER check would let this through"
    )
    assert kept.rereview_owed_base == before
    assert kept.rereview_candidate_sha == "", "nothing was carried, so nothing is named"

    approve(h, stale)

    assert "push_rereview_owed" in [b.code for b in h.blockers("t9")]
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == ""
    assert queued_review_packet(h) == "", "no packet: there is nothing new to review"
    refused = h.entries("postcommit_rereview_not_requested")
    assert [e["data"]["task_id"] for e in refused] == ["t9"]
    assert "carried-forward candidate" in refused[0]["data"]["reason"]
    assert h.execution_store.load("t9").rereview_owed_base == before, (
        "and the obligation is still owed, so the approval stays refused"
    )


def test_the_obligation_is_discharged_by_the_re_review_not_by_the_carry(tmp_path):
    """The marker survives a SUCCESSFUL carry-forward and is cleared where a
    new review packet is actually SENT — end to end, through the real loop
    rather than by writing the record a discharge would leave behind.

    A candidate carried onto a new head that nobody has looked at again is
    precisely what must stay unpushable, and the way it stops being that is a
    review the loop asks for itself: the approval taken against the old base
    publishes nothing and queues a fresh packet, that packet goes out bound to
    the CARRIED candidate, and only that new binding publishes it."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2)
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    stale = binding_for(nine, Path(nine.worktree_path))

    h.push("t1")

    carried = h.execution_store.load("t9")
    assert carried.rereview_owed_base, "still owed after the carry"
    assert carried.rereview_candidate_sha == carried.candidate_sha, (
        "and the carry NAMES what it produced, rather than leaving it to be inferred"
    )

    approve(h, stale)

    asked = h.execution_store.load("t9")
    assert asked.rereview_owed_base == "", "discharged where the packet was sent"
    assert asked.rereview_candidate_sha == ""
    assert asked.review_round == 1, "and that packet charged its own review round"
    assert [
        e["data"]["task_id"] for e in h.entries("postcommit_rereview_requested")
    ] == ["t9"]

    # The packet really goes out, and it binds to the CARRIED candidate: a
    # packet nothing could bind would spend the review and leave the approval
    # unable to publish anything.
    h.orch._step_ready()
    fresh = h.orch.state.pending_request.postcommit
    assert fresh is not None, "the re-review packet carries a binding"
    assert fresh.candidate_sha == carried.candidate_sha
    assert fresh.candidate_sha != stale.candidate_sha
    assert fresh.base_sha == h.head(), "reviewed against the base the merge left"

    # ONLY the new binding can publish. The old approval is still refused —
    # by `push_candidate_stale` now, since the obligation it named is
    # discharged — and publishes nothing.
    approve(h, stale)
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == ""
    assert [b.code for b in h.blockers("t9")] == ["push_candidate_stale"]

    approve(h, fresh)
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == carried.candidate_sha
    assert h.execution_store.load("t9").published_sha == carried.candidate_sha


def test_the_ask_never_outruns_the_review_round_cap(tmp_path):
    """The re-review is a review round like any other, so it is refused by the
    SAME cap that refuses a revision round — `_review_rounds_exhausted`, asked
    at both sites. A carry-forward moves its rounds to `carried_review_rounds`
    rather than discarding them precisely so this stays reachable: without that,
    a moved base would hand every task a fresh budget and the ask would be the
    one round that walks past a cap the rest of the loop enforces."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, max_review_rounds=1
    )
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    stale = binding_for(nine, Path(nine.worktree_path))

    h.push("t1")
    carried = h.execution_store.load("t9")
    assert (carried.review_round, carried.carried_review_rounds) == (0, 1)

    approve(h, stale)

    assert [b.code for b in h.blockers("t9")] == ["push_rereview_owed"]
    assert queued_review_packet(h) == "", "no packet was sent past the cap"
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == ""
    refused = h.entries("postcommit_rereview_not_requested")
    assert "no review rounds left" in refused[0]["data"]["reason"]
    assert h.execution_store.load("t9").rereview_owed_base, "still owed, still refused"


def test_a_candidate_under_a_split_ask_is_not_re_reviewed_over_the_top(tmp_path):
    """A packet nothing can bind is worse than a park: the review round is
    spent and the approval that comes back publishes nothing (the prof-01
    shape). `_current_pending_postcommit` refuses to bind a record holding a
    stat-only SPLIT ask, so this refuses to SEND one — the two halves of the
    same gate, met from opposite sides."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2)
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    stale = binding_for(nine, Path(nine.worktree_path))

    h.push("t1")
    carried = h.execution_store.load("t9")
    carried.attempt_ledger = (format_attempt(1, ATTEMPT_TASK, "sent_for_split_review"),)
    h.execution_store.save(carried)

    approve(h, stale)

    assert [b.code for b in h.blockers("t9")] == ["push_rereview_owed"]
    assert queued_review_packet(h) == ""
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == ""
    refused = h.entries("postcommit_rereview_not_requested")
    assert "SPLIT ask" in refused[0]["data"]["reason"]


def test_a_task_the_registry_lost_parks_rather_than_asking(tmp_path):
    """The packet names the task and quotes its title, so a record whose task
    is gone cannot be asked about. Park, loudly — a silent fall-through here
    would be indistinguishable from the ask being switched off."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2)
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    stale = binding_for(nine, Path(nine.worktree_path))

    h.push("t1")
    # The task leaves the registry AFTER the carry — an operator archiving a
    # row, or a registry file rewritten under a running fleet.
    h.orch._registry = TaskRegistry(
        [t for t in h.orch._registry.all_tasks() if t.id != "t9"]
    )

    approve(h, stale)

    assert [b.code for b in h.blockers("t9")] == ["push_rereview_owed"]
    assert queued_review_packet(h) == ""
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == ""
    refused = h.entries("postcommit_rereview_not_requested")
    assert "registry has no task" in refused[0]["data"]["reason"]


def test_an_unreadable_worker_parks_with_gits_own_words(tmp_path):
    """The packet is rendered from the worker repository, and a repository that
    cannot be read produces no packet. The refusal is BROAD on purpose — the
    park carries whatever git said rather than a guess at which failure it
    was — and it is still a refusal, so the old approval publishes nothing."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2)
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    stale = binding_for(nine, Path(nine.worktree_path))

    h.push("t1")
    carried = h.execution_store.load("t9")
    # The record still names its carried candidate; the repository it lives in
    # is what has gone.
    gone = tmp_path / "not-a-repo"
    gone.mkdir()
    carried.worktree_path = str(gone)
    h.execution_store.save(carried)

    approve(h, stale)

    assert [b.code for b in h.blockers("t9")] == ["push_rereview_owed"]
    assert queued_review_packet(h) == ""
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == ""
    refused = h.entries("postcommit_rereview_not_requested")
    assert "review packet could not be built" in refused[0]["data"]["reason"]
    assert h.execution_store.load("t9").rereview_owed_base, "still owed, still refused"


def test_at_one_lane_nothing_is_marked_and_the_merge_defers(tmp_path):
    """The single-lane path through the MERGER, asserted rather than assumed:
    the window is shut, the base does not move, no record is marked, and the
    in-flight candidate is exactly as it was."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=1)
    before = h.head()
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")

    h.push("t1")

    assert h.head() == before, "the base must not move while a candidate is bound"
    assert h.origin_base() == before
    kept = h.execution_store.load("t9")
    assert kept.rereview_owed_base == ""
    assert kept.candidate_sha == nine.candidate_sha
    assert kept.review_round == 1
    assert h.entries("auto_merge_rereview_owed") == []
    assert h.entries("auto_merge_candidate_carried_forward") == []


def enabled(config):
    """`config` with auto-merge on. The pushes above run with it OFF so
    published branches pile up unintegrated; the merge under test is then the
    one this returns a config for."""
    return dataclasses.replace(
        config, policy=dataclasses.replace(config.policy, auto_merge_enabled=True)
    )


def test_a_merge_with_no_way_to_carry_its_obligations_defers(tmp_path):
    """FAIL CLOSED on the missing collaborator. A process with no carry-forward
    wired — the startup sweep, which has no orchestrator — cannot resolve the
    observed clone a worker must fetch from, so it defers instead of moving a
    head it cannot repair behind."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, auto_merge_enabled=False
    )
    h.push("t1")                       # published, not integrated
    before = h.head()
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")

    config = enabled(h.config)
    outcome = auto_merge.AutoMerger(
        config=config,
        git=h.orch._git,
        policy=PolicyEngine(config.policy),
        execution_store=h.execution_store,
        registry=h.orch._registry,
        log=h.orch._log,
    ).attempt("t1")

    assert outcome == auto_merge.DEFERRED
    assert h.head() == before, "nothing was merged"
    kept = h.execution_store.load("t9")
    assert kept.rereview_owed_base == "", (
        "and nothing was marked either — the refusal is before any mutation"
    )
    assert kept.candidate_sha == nine.candidate_sha


def test_a_merge_that_conflicts_takes_the_mark_back(tmp_path):
    """The mark is written before the merge, so a merge that does NOT happen
    must give it back. A conflict aborts to the exact head it started from and
    `_abort` verifies that, so no base moved and nobody owes a re-review —
    leaving the mark would demand one over a merge that never landed."""
    h = build(tmp_path, per_task={"t1": {"README.md": "one\n"}}, lanes=2)
    resp = h.stage("t1")
    # The base moves the same file the candidate does: the merge below cannot
    # apply, and `README.md` is not a note tracker, so nothing auto-resolves it.
    (h.repo / "README.md").write_text("other\n", encoding="utf-8")
    run_git(h.repo, "add", "-A")
    run_git(h.repo, "commit", "-q", "-m", "the base takes the same file")
    before = h.head()
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")

    h.orch._dispatch_task_push(
        Directive(decision=Decision.PUSH, reason="approved"), resp
    )

    assert h.head() == before, "the merge aborted and the base is where it was"
    kept = h.execution_store.load("t9")
    assert kept.rereview_owed_base == "", "the mark was taken back"
    assert kept.candidate_sha == nine.candidate_sha
    assert kept.review_round == 1


def test_a_record_that_cannot_be_marked_defers_the_merge(tmp_path):
    """An obligation nothing recorded is one nothing downstream would enforce.
    A record that vanishes between the window walk and the mark is therefore a
    refusal, not a skip — and the refusal happens before the merge."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2)
    before = h.head()
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")

    real_load = h.execution_store.load

    def vanishing(task_id):
        if task_id == "t9":
            return None
        return real_load(task_id)

    h.orch._execution_store.load = vanishing
    try:
        h.push("t1")
    finally:
        h.orch._execution_store.load = real_load

    assert h.head() == before, "nothing was merged"
    assert h.origin_base() == before
    assert h.execution_store.load("t9").candidate_sha == nine.candidate_sha


# --- the sweep -----------------------------------------------------------------


def sweep(h, *, carry_forward):
    """`merge_sweep.sweep_backlog` over this harness, with auto-merge enabled
    for the sweep alone — the pushes above ran with it off, which is how three
    published branches pile up without being integrated one at a time."""
    return merge_sweep.sweep_backlog(
        enabled(h.config),
        git=h.orch._git,
        log=h.orch._log,
        carry_forward=carry_forward,
    )


def test_a_sweep_of_three_branches_re_evaluates_the_obligation_between_merges(tmp_path):
    """Each merge inside a sweep moves the base for every candidate that is not
    it, so the obligation cannot be computed once at the start: the candidate
    bound to the head before branch 1 is bound to a DIFFERENT head before
    branch 2. Three merges, three evaluations, three carries — and each one
    onto the head the merge before it produced."""
    h = build(
        tmp_path,
        per_task={"t1": {"a.py": "1\n"}, "t2": {"b.py": "2\n"}, "t3": {"c.py": "3\n"}},
        lanes=2,
        auto_merge_enabled=False,
    )
    before = h.head()
    for task_id in ("t1", "t2", "t3"):
        h.push(task_id)
    assert h.head() == before, "published, not integrated: the backlog"
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")

    result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)

    assert result.outcome == merge_sweep.SWEPT
    assert sorted(result.merged) == ["t1", "t2", "t3"]
    owed = [e["data"] for e in h.entries("auto_merge_rereview_owed")]
    carried = [e["data"] for e in h.entries("auto_merge_candidate_carried_forward")]
    assert [d["task_id"] for d in owed] == ["t9", "t9", "t9"], (
        "evaluated once per merge, not once for the sweep"
    )
    assert [d["task_id"] for d in carried] == ["t9", "t9", "t9"]
    # Each carry is onto the head the merge before it left, which is what
    # "between merges" means and what a single up-front evaluation cannot do.
    bases = [d["new_base"] for d in carried]
    assert len(set(bases)) == 3
    assert bases[-1] == h.head()
    final = h.execution_store.load("t9")
    assert final.task_base_sha == h.head()
    assert final.candidate_sha != nine.candidate_sha
    assert final.carried_review_rounds == 1, (
        "one review round existed and it was carried once, not multiplied"
    )


def test_a_refused_carry_forward_does_not_halt_the_sweep(tmp_path):
    """The all-or-nothing property is about the SWEEP's own branches, and it is
    unchanged. A carry-forward that refuses parks the task it is about and
    contributes nothing to the merge's outcome — turning it into a stop would
    leave a backlog half-swept over a third task's bookkeeping.

    It also pins the second half of "re-evaluated between merges": the
    obligation is minted from the record as it stands before EACH merge, so a
    refusal that leaves the record behind the head produces exactly one, not one
    per branch."""
    h = build(
        tmp_path,
        per_task={"t1": {"a.py": "1\n"}, "t2": {"b.py": "2\n"}, "t3": {"c.py": "3\n"}},
        lanes=2,
        auto_merge_enabled=False,
    )
    for task_id in ("t1", "t2", "t3"):
        h.push(task_id)
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    # A worker repository that is not there any more: every carry-forward
    # refuses, on every one of the three merges.
    nine.worktree_path = ""
    h.execution_store.save(nine)

    result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)

    assert result.outcome == merge_sweep.SWEPT
    assert sorted(result.merged) == ["t1", "t2", "t3"]
    assert result.stopped_on == ""
    refusals = h.entries("auto_merge_carry_forward_refused")
    assert [e["data"]["task_id"] for e in refusals] == ["t9"]
    # ONCE, and the re-evaluation is why: the refusal left the record on its
    # ORIGINAL base, so the second and third merges find it a proper ancestor of
    # the head and report it as already-behind rather than minting a second
    # obligation. Moving a head cannot strand it any further than the first one
    # already did — the note arm this predicate has had since 2026-08-21.
    assert [e["data"]["task_id"] for e in h.entries("auto_merge_rereview_owed")] == ["t9"]
    parked = [b for b in h.blockers("t9") if b.code == "task_base_behind_head"]
    assert len(parked) == 1
    assert h.execution_store.load("t9").rereview_owed_base, (
        "and it still owes a re-review, so its approval is still refused"
    )


def test_a_sweep_with_no_carry_forward_merges_nothing_and_stops(tmp_path):
    """The startup sweep's shape. It cannot discharge an obligation, so the
    first branch defers — and the sweep's own rule takes over from there:
    every branch behind it is left untouched and named."""
    h = build(
        tmp_path,
        per_task={"t1": {"a.py": "1\n"}, "t2": {"b.py": "2\n"}},
        lanes=2,
        auto_merge_enabled=False,
    )
    for task_id in ("t1", "t2"):
        h.push(task_id)
    before = h.head()
    bound_candidate(h, "t9", "nine.py", "nine\n")

    result = sweep(h, carry_forward=None)

    assert result.outcome == merge_sweep.STOPPED
    assert result.merged == []
    assert h.head() == before, "nothing moved"
    assert h.execution_store.load("t9").rereview_owed_base == ""


# --- the budget the reset must not refill --------------------------------------


def test_a_carried_forward_record_still_reads_as_reviewed(tmp_path):
    """`review_round` is reset, so anything reading it alone would send this
    record down the re-base branch — which quarantines the worker and blanks
    `candidate_sha`. `carried_review_rounds` is what stops the guard switching
    itself off on exactly the record a moved base just carried."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2)
    bound_candidate(h, "t9", "nine.py", "nine\n")
    h.push("t1")

    # The head moves again, this time with nobody merging: an operator commit.
    (h.repo / "elsewhere.txt").write_text("someone else\n")
    run_git(h.repo, "add", "-A")
    run_git(h.repo, "commit", "-q", "-m", "operator")

    carried = h.execution_store.load("t9")
    assert (carried.review_round, carried.carried_review_rounds) == (0, 1)
    survivor = h.orch._rebase_execution_if_stale(carried, h.orch._registry.get("t9"))

    assert survivor is not None, "the reviewed record is carried, never re-based"
    assert survivor.candidate_sha, "a re-base would have blanked this"
    assert h.execution_store.load("t9").candidate_sha == survivor.candidate_sha


def test_the_round_cap_counts_the_rounds_a_carry_forward_moved(tmp_path):
    """A moving base must refill no budget. `review_round` is 0 on this record
    and its two rounds are all in `carried_review_rounds`, so a cap that read
    only the former would dispatch a third round it has no allowance for."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, max_review_rounds=2
    )
    nine = bound_candidate(h, "t9", "nine.py", "nine\n", review_round=0)
    nine.carried_review_rounds = 2
    h.execution_store.save(nine)

    h.orch._dispatch_task_postcommit(
        Directive(decision=Decision.REVISE, reason="again", task_id="t9"),
        h.orch._registry.get("t9"),
        h.orch.state,
    )

    assert [b.code for b in h.blockers("t9")] == ["review_round_cap"]
