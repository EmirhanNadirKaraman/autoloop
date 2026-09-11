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

conc-13 finishes the same conversion for the OTHER fleet-wide mutual exclusion
in that predicate — "a phase is executing", read from LANE 0's state file, which
was unsound (one lane, speaking for the fleet) and starving (lane 0 is executing
nearly always, so at N lanes the window essentially never opened: 5.5 hours on
2026-09-09 with the base unmoved). Its tests are the two window-predicate cases
below and the section at the end of this file, which walks the lane states that
clause was standing in for — a bound candidate, a DIRTY worker, a round with no
candidate yet — with every lane's own state file set to `executing`.

That clause was also buying SERIALISATION, by accident: while it held the window
shut whenever a round was mid-write, two lanes could not both be inside
`AutoMerger.after_completion`, which every lane reaches the moment it publishes
and which merges into the one shared checkout. The last section of this file
pins the fleet merge token that buys it back — the same file `merge_sweep` takes
— by driving a second lane's completion from inside the first lane's
`merge_commit`, which is the interleaving rather than a hope of one.

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

import argparse
import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from autoloop import auto_merge, cli, merge_sweep
from autoloop.auto_merge import MergeObligation
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, ConcurrencyConfig, lane_id
from autoloop.contract import Decision, Directive
from autoloop.errors import GitCommandError, LockHeldError, StateCorruptError
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
    lane_paths,
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


def executing(config, *lane_indices):
    """Put each named lane into `Phase.EXECUTING` in that lane's OWN state file.

    `lane_paths` rather than `config.state_file`, and that IS the point of the
    tests below: lane 0's state file is literally `state.json` while lane k>0
    lives under `lanes/<lane_id>/`, so a predicate that reads only the first one
    cannot see the second one at all. Writing both is what makes "the window
    opens while lanes are executing" a claim about the FLEET rather than about
    whichever lane happens to own `state.json`.
    """
    for index in lane_indices:
        paths = lane_paths(config.state_dir, index)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        StateStore(paths.state_file).save(
            LoopState(
                session_id=f"s{index}",
                conversation_url=URL,
                phase=Phase.EXECUTING.value,
            )
        )


def test_at_one_lane_an_executing_phase_still_shuts_the_window(tmp_path):
    """THE acceptance criterion for the clause conc-13 converted, pinned as a
    literal for `test_at_one_lane_a_bound_candidate...`'s reason: at one lane
    `state.json` IS the loop, an agent may be mid-write, and the whole sentence
    an operator reads must be the one this function has always produced.

    NO execution record, deliberately: the two blockers are independent and both
    fire at one lane, so a record here would make this an assertion about the
    bound-candidate reason as well and stop pinning either one exactly. The pair
    is asserted next, and the two-lane case after that is where a record IS
    needed — there it becomes the note, leaving this reason as the only thing
    that could still have held the window."""
    config = window_config(tmp_path, lanes=1)
    register(config, "t9")
    placer = _Placer()
    executing(config, 0)

    reasons, notes = cli._merge_window_blockers(config, set(), placer)

    assert reasons == ["a phase is executing — an agent may be mid-write"]
    assert notes == []


def test_at_one_lane_both_blockers_fire_together_and_stay_independent(tmp_path):
    """And with a record, at one lane, BOTH — in this order. The clause conc-13
    converted never subsumed the bound-candidate one and still does not: a lane
    mid-write and a candidate pinned to the head are two different facts about
    the same instant, and the one-lane predicate reports both exactly as it
    always has."""
    config = window_config(tmp_path, lanes=1)
    register(config, "t9")
    placer = _Placer()
    write_record(config, "t9", base=placer.head)
    executing(config, 0)

    reasons, _notes = cli._merge_window_blockers(config, set(), placer)

    assert len(reasons) == 2
    assert reasons[0].startswith("task t9 has a candidate") and "strand it" in reasons[0]
    assert reasons[1] == "a phase is executing — an agent may be mid-write"


def test_above_one_lane_executing_lanes_do_not_shut_the_window(tmp_path):
    """conc-13, and the direct falsifier of what this predicate did before it.

    EVERY lane is mid-write here, which is the ordinary steady state of a
    two-lane fleet — measured 2026-09-09: 5.5 hours, two tasks completed, and
    the base did not advance once. The record bound to the head still owes its
    re-review; nothing about the fleet's phases withholds the window on top of
    that."""
    config = window_config(tmp_path, lanes=2)
    register(config, "t9")
    placer = _Placer()
    (tmp_path / "w9").mkdir()
    write_record(config, "t9", base=placer.head, worktree_path=str(tmp_path / "w9"))
    executing(config, 0, 1)

    obligations: list = []
    reasons, notes = cli._merge_window_blockers(
        config, set(), placer, obligations=obligations
    )

    assert reasons == [], "the window OPENS with both lanes mid-write"
    assert [o.task_id for o in obligations] == ["t9"], (
        "and the candidate bound to the head still owes its re-review"
    )
    assert len(notes) == 1 and "OWES A RE-REVIEW" in notes[0]


def test_above_one_lane_no_lane_state_file_is_read_at_all(tmp_path):
    """Decision 7's rule, made CHECKABLE rather than asserted in prose.

    `state.json` is lane 0's, so any phase read from it could only ever be one
    lane's — and the old clause read exactly that and spoke for the fleet. The
    proof that it is no longer read is a lane 0 state file that CANNOT be read:
    `StateStore.load` raises `StateCorruptError` on it, so a window that still
    consulted it could not return at all. A survey-for-a-note would fail this
    test too, deliberately — it would put the same unreadable file back on the
    path of a predicate that has no need of it."""
    config = window_config(tmp_path, lanes=2)
    register(config, "t9")
    config.state_file.write_text("{not json", encoding="utf-8")

    reasons, notes = cli._merge_window_blockers(config, set(), _Placer())

    assert (reasons, notes) == ([], [])


def test_at_one_lane_the_state_file_is_still_read_and_still_refuses(tmp_path):
    """The other side of the same fact, so "not read above one lane" cannot be
    mistaken for "not read at all". At one lane the file is the loop's own, and
    an unreadable one raises exactly as it always did — every caller here wraps
    that in a fail-closed `except` (`_shelved_candidate_window_report`,
    `_discarded_candidate_window_report`, `merge_sweep`'s gate) and reads it as
    SHUT."""
    config = window_config(tmp_path, lanes=1)
    register(config, "t9")
    config.state_file.write_text("{not json", encoding="utf-8")

    with pytest.raises(StateCorruptError):
        cli._merge_window_blockers(config, set(), _Placer())


def test_the_command_says_what_OPEN_means_at_each_lane_count(tmp_path, monkeypatch, capsys):
    """The operator-facing half, and the same defect class as the `health.py`
    line this round corrects. "No unpublished candidate, no executing phase" is
    what OPEN means at one lane; above one lane NEITHER clause is checked any
    more, so printing it there would be this command asserting exactly the two
    things it stopped asking. No execution records in either half, deliberately:
    with none, nothing reaches the gateway, so this is a claim about the
    sentence and nothing else."""
    args = argparse.Namespace(config=None, wait=False, timeout=0.1, poll=0.01)

    one = window_config(tmp_path / "one", lanes=1)
    monkeypatch.setattr(cli, "load_config", lambda _p: one)

    assert cli._cmd_merge_window(args) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        "merge window OPEN — no unpublished candidate, no executing phase"
    ), "byte for byte at one lane"

    two = window_config(tmp_path / "two", lanes=2)
    monkeypatch.setattr(cli, "load_config", lambda _p: two)

    assert cli._cmd_merge_window(args) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert line.startswith("merge window OPEN — at 2 lanes")
    assert "no executing phase" not in line, "it did not look, so it must not say"
    assert "no unpublished candidate" not in line


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


# --- the executing-phase clause, converted (conc-13) ---------------------------
#
# The window predicate's own half is in the first section of this file, beside
# the bound-candidate cases. What follows is the same claim END TO END, over
# real repositories: with EVERY lane mid-write — the state that shut the window
# for 5.5 hours on 2026-09-09 — the backlog drains, and each lane state the old
# clause was standing in for is answered by the machinery that is more precise
# than it was. One test per lane state, and `executing(...)` is written into
# every lane's own state file in all of them, because a claim about the fleet
# that only ever sets lane 0's file is a claim about lane 0.


def test_a_sweep_lands_a_branch_with_every_lane_executing(tmp_path):
    """THE measured failure, inverted. Two lanes, both mid-write, one completed
    task published and unintegrated: the sweep merges it and the base moves.

    Before conc-13 this deferred on the very first gate check — `merge_sweep`
    checks the window once for the whole backlog, and lane 0's phase shut it —
    so the branch waited for an operator to merge it by hand."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "1\n"}}, lanes=2, auto_merge_enabled=False
    )
    before = h.head()
    h.push("t1")                       # published, not integrated: the backlog
    assert h.head() == before
    landed = h.execution_store.load("t1").candidate_sha
    executing(h.config, 0, 1)

    result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)

    assert result.outcome == merge_sweep.SWEPT
    assert result.merged == ["t1"]
    assert h.head() != before, "the base advanced, which is the whole claim"
    assert contains(h.repo, h.head(), landed)
    assert h.origin_base() == h.head(), "and it was pushed, not merged locally"


def test_at_one_lane_an_executing_phase_still_defers_the_whole_sweep(tmp_path):
    """The acceptance criterion end to end, and the reason the test above is
    not simply "sweeps work now". At one lane `state.json` IS the loop, the
    clause is the one it has always been, and the sweep defers on it with
    today's reason — nothing was merged and nothing was pushed."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "1\n"}}, lanes=1, auto_merge_enabled=False
    )
    h.push("t1")
    before = h.head()
    executing(h.config, 0)

    result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)

    assert result.outcome == merge_sweep.DEFERRED
    assert result.reasons == ["a phase is executing — an agent may be mid-write"]
    assert result.merged == []
    assert h.head() == before and h.origin_base() == before


def test_with_every_lane_executing_a_bound_candidate_is_carried_and_re_asked(tmp_path):
    """LANE STATE: a reviewed candidate bound to the head, in a lane that is
    mid-write. The base moves under it and it is not stranded — carried onto the
    new head, its review round reset, and the approval taken against the old
    base publishes nothing and asks for a new review instead."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, auto_merge_enabled=False
    )
    h.push("t1")
    before = h.head()
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    worktree = Path(nine.worktree_path)
    stale = binding_for(nine, worktree)
    executing(h.config, 0, 1)

    result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)

    assert result.merged == ["t1"]
    assert h.head() != before
    carried = h.execution_store.load("t9")
    assert carried.task_base_sha == h.head()
    assert carried.candidate_sha != nine.candidate_sha
    assert contains(worktree, carried.candidate_sha, nine.candidate_sha), (
        "a MERGE: the reviewed commit still exists and is still reachable"
    )
    assert carried.review_round == 0, "reset, so the loop ASKS for the new review"
    assert carried.carried_review_rounds == 1, "and no budget was refilled"
    assert carried.rereview_owed_base == before

    approve(h, stale)

    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == "", (
        "never pushed on its old approval"
    )
    assert h.blockers("t9") == [], "asked, not parked"
    assert carried.candidate_sha in queued_review_packet(h)


def test_with_every_lane_executing_an_uncarryable_candidate_still_parks(tmp_path):
    """LANE STATE: a reviewed candidate the head cannot be merged into — both
    branches take the same file. The sweep still lands its own branch (the
    all-or-nothing property is about the SWEEP's branches, unchanged), and the
    candidate that cannot be carried parks `task_base_behind_head` rather than
    being merged or pushed on the approval it already had."""
    h = build(
        tmp_path,
        per_task={"t1": {"shared.py": "one\n"}},
        lanes=2,
        auto_merge_enabled=False,
    )
    h.push("t1")
    before = h.head()
    nine = bound_candidate(h, "t9", "shared.py", "nine\n")
    worktree = Path(nine.worktree_path)
    tip_before = head(worktree)
    stale = binding_for(nine, worktree)
    executing(h.config, 0, 1)

    result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)

    assert result.merged == ["t1"]
    parks = [b for b in h.blockers("t9") if b.code == "task_base_behind_head"]
    assert len(parks) == 1
    # Nothing was discarded to buy that park.
    assert is_clean(worktree), "the carry-forward's own merge was aborted"
    assert head(worktree) == tip_before
    kept = h.execution_store.load("t9")
    assert (kept.candidate_sha, kept.task_base_sha) == (nine.candidate_sha, before)
    assert kept.review_round == 1
    assert kept.rereview_owed_base == before

    approve(h, stale)

    assert "push_rereview_owed" in [b.code for b in h.blockers("t9")]
    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == ""


def test_a_lane_mid_write_loses_no_work_when_the_base_moves_under_it(tmp_path):
    """LANE STATE: an agent literally mid-write — a worker repository with
    uncommitted changes, which is what "a phase is executing" was standing in
    for and the one thing it could not actually see (it read lane 0's phase, not
    any lane's tree).

    The dirty tree is the guard, and it is precondition 4 of the carry-forward:
    merging over that residue is exactly the quiet discard the refusal exists to
    prevent. So the merge lands, the carry refuses, the task parks, and every
    byte the agent had written is still there."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, auto_merge_enabled=False
    )
    h.push("t1")
    before = h.head()
    nine = bound_candidate(h, "t9", "nine.py", "nine\n")
    worktree = Path(nine.worktree_path)
    tip_before = head(worktree)
    stale = binding_for(nine, worktree)
    # THE MID-WRITE: a revise round is under way in this lane's worker — the
    # reviewed candidate is still what the record names, and an agent is part
    # way through the next one.
    (worktree / "nine.py").write_text("half of the next round\n", encoding="utf-8")
    (worktree / "brand-new.py").write_text("a file it just made\n", encoding="utf-8")
    executing(h.config, 0, 1)

    result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)

    assert result.merged == ["t1"], "the fleet's backlog still drains"
    # NOT ONE BYTE of the in-flight round was touched.
    assert (worktree / "nine.py").read_text() == "half of the next round\n"
    assert (worktree / "brand-new.py").read_text() == "a file it just made\n"
    assert head(worktree) == tip_before
    assert run_git(worktree, "branch", "--show-current").strip() == nine.task_branch
    # And the refusal names the reason rather than being silent about it.
    refusals = [e["data"] for e in h.entries("auto_merge_carry_forward_refused")]
    assert [d["task_id"] for d in refusals] == ["t9"]
    assert "uncommitted changes" in refusals[0]["reason"]
    assert len([b for b in h.blockers("t9") if b.code == "task_base_behind_head"]) == 1
    kept = h.execution_store.load("t9")
    assert (kept.candidate_sha, kept.task_base_sha) == (nine.candidate_sha, before)
    assert kept.rereview_owed_base == before

    approve(h, stale)

    assert ref_sha(h.origin, f"refs/heads/{nine.task_branch}") == "", (
        "the approval taken against the old base still publishes nothing"
    )


def test_a_lane_with_no_candidate_yet_is_not_stranded_by_the_moving_base(tmp_path):
    """LANE STATE: a round that has dispatched but not committed, so its record
    names no candidate at all. It is skipped by the window for want of one — and
    that is safe for a reason the phase clause never supplied: the worker is a
    SEPARATE repository, so a merge into this checkout does not reach it, and
    nothing reviewed exists to strand. What happens NEXT is
    `_rebase_execution_if_stale`'s unreviewed arm — unchanged by this round, and
    reached because the record stays unreviewed, which is asserted here rather
    than driven (that arm rebuilds the worker through `_worker_repos`, which
    this harness does not wire)."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, auto_merge_enabled=False
    )
    h.push("t1")
    before = h.head()
    nine = bound_candidate(h, "t9", "nine.py", "nine\n", review_round=0)
    worktree = Path(nine.worktree_path)
    nine.candidate_sha = ""            # dispatched, nothing committed yet
    nine.candidate_commit_count = 0
    h.execution_store.save(nine)
    (worktree / "nine.py").write_text("mid-write\n", encoding="utf-8")
    executing(h.config, 0, 1)

    result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)

    assert result.merged == ["t1"]
    assert h.head() != before
    # Nothing was marked, nothing was carried, nothing was parked — and the
    # worker's uncommitted work is exactly where the agent left it.
    assert h.entries("auto_merge_rereview_owed") == []
    assert h.blockers("t9") == []
    assert (worktree / "nine.py").read_text() == "mid-write\n"
    stale = h.execution_store.load("t9")
    assert stale.rereview_owed_base == ""
    assert (stale.review_round, stale.carried_review_rounds) == (0, 0), (
        "unreviewed, which is what routes the next dispatch to the re-base arm "
        "rather than to the carry-forward or the park"
    )
    assert stale.task_base_sha == before, "still on the base it was cut from"


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


# --- and two lanes merging at once (conc-13) -----------------------------------
#
# The clause converted above was buying ONE thing the per-candidate obligation
# does not: while it held the window shut whenever a round was mid-write, two
# lanes could not both be inside `AutoMerger.after_completion` — the path EVERY
# lane takes the moment it publishes, in its own process, into the ONE shared
# checkout the whole fleet builds against. Opening the window made those
# simultaneous, and simultaneous is `index.lock`, a merge verified against a head
# the sibling has already moved, or one lane's `merge --abort` unwinding the
# other's. What follows pins the token that buys it back — the SAME file the
# sweep takes — and the acceptance criterion that no such file exists at one
# lane.


def merger(h, *, lane_index):
    """One lane's `AutoMerger` over the shared checkout, wired exactly as
    `orchestrator._auto_merge_after_completion` wires it — same config, same
    gateway, same stores, and its own lane index. Two of these ARE two lanes as
    far as that checkout is concerned: the fleet's serialisation is a file under
    the state dir, not an object either of them holds."""
    config = enabled(h.config)
    return auto_merge.AutoMerger(
        config=config,
        git=h.orch._git,
        policy=PolicyEngine(config.policy),
        execution_store=h.execution_store,
        registry=h.orch._registry,
        log=h.orch._log,
        carry_forward=h.orch._carry_candidate_past_for_merge,
        lane_index=lane_index,
    )


def token_file(h):
    return merge_sweep.merge_token_file(h.config.state_dir)


def test_a_second_lanes_completion_cannot_enter_the_merge_of_the_first(tmp_path):
    """THE regression, forced rather than raced for.

    Lane 1's completion is driven from INSIDE lane 0's `merge_commit` — the one
    instant where a second mutation of that checkout does the damage, and the
    interleaving a thread test can only hope to hit. Lane 1 defers, moves
    nothing, leaves no residue and aborts nothing; lane 0's merge lands and is
    pushed; and lane 1 merges on its own next completion with nothing lost.
    """
    h = build(
        tmp_path,
        per_task={"t1": {"a.py": "one\n"}, "t2": {"b.py": "two\n"}},
        lanes=2,
        auto_merge_enabled=False,
    )
    h.push("t1")
    h.push("t2")                       # both published, neither integrated
    before = h.head()
    assert not token_file(h).exists()
    one = h.execution_store.load("t1").candidate_sha
    two = h.execution_store.load("t2").candidate_sha

    lane0, lane1 = merger(h, lane_index=0), merger(h, lane_index=1)
    inner: dict = {}
    entered: list = []
    real_merge = h.orch._git.merge_commit

    def racing_merge(candidate, message):
        # The flag is raised BEFORE the re-entrant call, not after it: were the
        # sibling ever to reach a merge of its own, this hook would otherwise
        # call itself forever instead of failing the assertion below.
        if not entered:                # lane 0's own merge, once
            entered.append(True)
            inner["outcome"] = lane1.after_completion("t2")
            inner["head"] = h.head()
            inner["clean"] = is_clean(h.repo)
            inner["token"] = token_file(h).exists()
        return real_merge(candidate, message)

    h.orch._git.merge_commit = racing_merge
    try:
        outcome = lane0.after_completion("t1")
    finally:
        h.orch._git.merge_commit = real_merge

    # 1. The sibling was refused, and refused for THIS reason rather than by
    #    accident of some other precondition.
    assert inner["outcome"] == {"t2": auto_merge.DEFERRED}
    refusals = [
        e["data"] for e in h.entries("auto_merge_deferred")
        if e["data"]["task_id"] == "t2"
    ]
    assert len(refusals) == 1
    assert "another lane is merging" in refusals[0]["reason"]
    assert "merge token" in refusals[0]["reason"]
    # 2. It overlapped no mutation: the head lane 0 was merging onto was still
    #    the head, the checkout was clean, and the token was lane 0's.
    assert inner["head"] == before
    assert inner["clean"]
    assert inner["token"], "lane 0 was holding it, which is why lane 1 was refused"
    # 3. And it aborted nothing — lane 0's merge landed and was pushed.
    assert h.entries("auto_merge_conflict") == []
    assert outcome == {"t1": auto_merge.MERGED}
    assert h.head() != before
    assert contains(h.repo, h.head(), one)
    assert h.origin_base() == h.head()
    assert not token_file(h).exists(), "the token went back"

    # 4. Nothing was lost: the deferred lane merges on its own next completion.
    assert lane1.after_completion("t2") == {"t2": auto_merge.MERGED}
    assert contains(h.repo, h.head(), two)
    assert h.origin_base() == h.head()
    assert not token_file(h).exists()


def test_at_one_lane_a_completion_takes_no_token_at_all(tmp_path):
    """THE acceptance criterion, asserted DURING the merge rather than after it.

    A token created and released again also satisfies "no file afterwards", and
    that is not the claim: at one lane there is nothing to serialise, so the
    completion path must never build the object, construct the path or write the
    file. The observation is taken from inside `merge_commit`, which is the one
    moment the file would exist if it were ever written."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=1)
    before = h.head()
    seen: dict = {}
    real_merge = h.orch._git.merge_commit

    def watching_merge(candidate, message):
        seen["token"] = token_file(h).exists()
        return real_merge(candidate, message)

    h.orch._git.merge_commit = watching_merge
    try:
        h.push("t1")                   # through the orchestrator, real wiring
    finally:
        h.orch._git.merge_commit = real_merge

    assert seen["token"] is False, "no token exists while a single lane merges"
    assert not token_file(h).exists()
    assert h.head() != before, "and the merge still landed"
    assert h.origin_base() == h.head()


def test_at_one_lane_a_stray_token_file_is_never_even_looked_at(tmp_path):
    """The other half of that criterion. A `merge_token.json` an experiment left
    behind is unreadable bytes — which above one lane is a REFUSAL, deliberately
    (`MergeToken.read` fails closed). At one lane the completion path does not
    read it, does not repair it and does not defer on it, and the file is exactly
    as it was afterwards."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=1)
    before = h.head()
    stray = token_file(h)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("{not json", encoding="utf-8")

    h.push("t1")

    assert h.head() != before
    assert h.origin_base() == h.head()
    assert stray.read_text(encoding="utf-8") == "{not json"


def test_a_completion_defers_while_a_sibling_lane_holds_the_merge_token(tmp_path):
    """The same refusal from the other direction: the holder is a token file a
    live lane wrote, not a re-entrant call. Nothing merges, nothing is pushed,
    the sibling's token is neither stolen nor rewritten, and the deferral is
    RECORDED — so the next completion drains it rather than the work waiting for
    a sweep to notice."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, auto_merge_enabled=False
    )
    h.push("t1")
    before = h.head()
    held = merge_sweep.MergeToken(h.config.state_dir, lane_id(1)).acquire()
    fingerprint = token_file(h).read_text(encoding="utf-8")

    try:
        outcome = merger(h, lane_index=0).after_completion("t1")

        assert outcome == {"t1": auto_merge.DEFERRED}
        assert h.head() == before and h.origin_base() == before
        assert token_file(h).read_text(encoding="utf-8") == fingerprint
        recorded = auto_merge.MergeDeferralStore(
            h.config.merge_deferrals_dir
        ).all_deferrals()
        assert [d.task_id for d in recorded] == ["t1"]
        assert "another lane is merging" in recorded[0].reason
    finally:
        held.release()

    assert merger(h, lane_index=0).after_completion("t1") == {"t1": auto_merge.MERGED}
    assert h.head() != before and h.origin_base() == h.head()


def test_a_merge_token_that_cannot_be_read_defers_rather_than_merging(tmp_path):
    """FAIL CLOSED, and this is the fail-open a token invites: bytes nobody can
    parse read as "nobody holds it" would be two lanes merging with nothing
    saying so. `MergeToken.read` raises, `take_merge_token` turns that into a
    refusal, and the completion defers with the base where it was."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, auto_merge_enabled=False
    )
    h.push("t1")
    before = h.head()
    token_file(h).write_text("{not json", encoding="utf-8")

    outcome = merger(h, lane_index=0).after_completion("t1")

    assert outcome == {"t1": auto_merge.DEFERRED}
    assert h.head() == before and h.origin_base() == before
    assert token_file(h).read_text(encoding="utf-8") == "{not json", "not stolen"


def test_the_token_goes_back_when_the_merge_raises_out_of_the_gateway(tmp_path):
    """The `finally` is the whole of it. A token returned only on the paths
    somebody remembered would leave the fleet unable to merge until a lane died
    and was recovered — a worse failure than the one it prevents — so an
    exception from inside the merge still gives it back, and the next completion
    merges."""
    h = build(
        tmp_path, per_task={"t1": {"a.py": "one\n"}}, lanes=2, auto_merge_enabled=False
    )
    h.push("t1")
    before = h.head()
    real_merge = h.orch._git.merge_commit

    def exploding_merge(candidate, message):
        raise RuntimeError("the gateway fell over")

    h.orch._git.merge_commit = exploding_merge
    try:
        outcome = merger(h, lane_index=0).after_completion("t1")
    finally:
        h.orch._git.merge_commit = real_merge

    assert outcome == {"t1": auto_merge.FAILED}
    assert h.head() == before
    assert not token_file(h).exists(), "the token went back anyway"
    assert merger(h, lane_index=0).after_completion("t1") == {"t1": auto_merge.MERGED}


def test_the_token_is_the_same_file_the_sweep_takes(tmp_path):
    """ONE FILE, ONE GATE, asserted in the direction the other tests do not
    cover. A completion holding a token of its own and a sweep holding another
    would serialise each kind against itself and neither against the other —
    with an extra file in the way of noticing.

    The path is the assertion: `test_a_second_lanes_completion_...` observes
    `merge_token_file(state_dir)` EXISTING while a completion merges, so the
    completion writes that file and no other, and this shows a sweep refusing on
    exactly it. Written as one held token rather than as a re-entrant sweep
    inside a merge, because the claim is about which file each side reads and a
    sweep driven from inside another merger's `merge_commit` would buy nothing
    but a second way to hang."""
    h = build(
        tmp_path,
        per_task={"t1": {"a.py": "one\n"}, "t2": {"b.py": "two\n"}},
        lanes=2,
        auto_merge_enabled=False,
    )
    h.push("t1")
    h.push("t2")
    before = h.head()
    # A lane mid-completion, as far as this file is concerned.
    held = merge_sweep.MergeToken(h.config.state_dir, lane_id(0)).acquire()
    try:
        result = sweep(h, carry_forward=h.orch._carry_candidate_past_for_merge)
    finally:
        held.release()

    assert result.outcome == merge_sweep.DEFERRED
    assert "merge token" in result.reasons[0]
    assert result.merged == []
    assert h.head() == before and h.origin_base() == before


# --- the token's own gate, without a repository ---------------------------------
#
# The claim in these is a pure decision over a config and one file, so none of
# them builds a repository: the cost of a real one buys nothing when nothing git
# does is in question.


def test_at_one_lane_no_token_object_is_built_at_all(tmp_path, monkeypatch):
    """The acceptance criterion at its narrowest, and structurally: at one lane
    `take_merge_token` must not construct a `MergeToken`, derive its path or
    touch the state dir. A constructor that fails the test if it is reached
    proves that more exactly than an absent file does — the file is also absent
    after a token that was created and released."""
    config = window_config(tmp_path, lanes=1)
    monkeypatch.setattr(
        merge_sweep,
        "MergeToken",
        lambda *a, **k: pytest.fail("no token object may be built at one lane"),
    )

    assert merge_sweep.take_merge_token(config, 0) == (None, "")
    assert not merge_sweep.merge_token_file(config.state_dir).exists()


def test_the_token_gate_and_the_window_read_the_same_lane_count(tmp_path):
    """THE PAIRING, and the fail-open it closes. The window decides whether it
    may open above one lane from `config.concurrency.lanes > 1`; if the token
    gate ever read that differently, a window could open as a fleet while the
    token was skipped as a single lane — two lanes merging with nothing saying
    so. `merges_are_serialised` is the union of both readings, so a value the
    defensive reading calls one lane and the window calls a fleet is serialised
    rather than waved through."""
    merges = merge_sweep.merges_are_serialised
    assert merges(window_config(tmp_path, lanes=1)) is False
    assert merges(window_config(tmp_path, lanes=2)) is True
    # The window's own reading, for values `_fleet_lanes` alone would call one
    # lane. `2.5 > 1` opens the window; this must not skip the token.
    odd = dataclasses.replace(
        window_config(tmp_path, lanes=1),
        concurrency=ConcurrencyConfig(lanes=2.5),
    )
    assert merges(odd) is True
    # And a count nothing can COMPARE is a fleet, fail-closed: "could not tell"
    # is never "one lane".
    unreadable = dataclasses.replace(
        window_config(tmp_path, lanes=1),
        concurrency=ConcurrencyConfig(lanes="two"),
    )
    assert merges(unreadable) is True


def test_a_token_the_race_keeps_losing_defers_instead_of_recursing(
    tmp_path, monkeypatch
):
    """The retry is BOUNDED. "The file existed, and by the time it was read it
    did not" is a sibling releasing between two syscalls, and one more attempt
    is the right answer — but the branch used to spell that as a self-call with
    no bound on it, and conc-13 gave it a second caller that reaches it on every
    completion instead of once per sweep. A lane that loses the race every time
    is CONTENDED rather than unlucky: it gets the refusal every caller already
    turns into a deferral, not a stack that grows until `RecursionError` leaves
    by a path neither caller catches."""
    config = window_config(tmp_path, lanes=2)
    token = merge_sweep.MergeToken(config.state_dir, lane_id(1))
    # The file is REALLY there, so `O_EXCL` really fails; `read` answering
    # `None` is the sibling that released between the two syscalls, every time.
    # Built this way rather than by patching `os.open`, which is process-wide
    # and would sit under everything else running in this worker.
    token.path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(merge_sweep.MergeToken, "read", lambda self: None)

    with pytest.raises(LockHeldError) as caught:
        token.acquire()

    assert "faster than this lane can claim it" in str(caught.value)
    assert token.path.read_text(encoding="utf-8") == "{}", "and nothing was stolen"


def test_the_token_gate_refuses_rather_than_raising_whatever_goes_wrong(
    tmp_path, monkeypatch
):
    """FAIL CLOSED AND QUIETLY. Both callers take the token OUTSIDE their own
    `try/finally` and both are documented never to raise, so anything escaping
    here leaves `sweep` or `after_completion` by traceback in a caller whose
    push has already landed. Refusing is also the closed direction, which is
    what makes catching this widely safe: the outcome is always "this lane does
    not merge", never "this lane merges without the token"."""
    config = window_config(tmp_path, lanes=2)

    def falls_over(self):
        raise RuntimeError("the filesystem fell over")

    monkeypatch.setattr(merge_sweep.MergeToken, "acquire", falls_over)

    token, refusal = merge_sweep.take_merge_token(config, 0)

    assert token is None
    assert "RuntimeError" in refusal and "the filesystem fell over" in refusal


def test_releasing_a_token_swallows_what_it_cannot_remove(tmp_path, monkeypatch):
    """The other half of the same contract. `release_merge_token` runs in a
    `finally` in both callers, so an `unlink` that fails for a reason
    `MergeToken.release` does not already tolerate would turn a merge that
    LANDED into an exception its caller reports as a failure. `None` — every
    single-lane caller — is a no-op that touches nothing."""
    config = window_config(tmp_path, lanes=2)
    token = merge_sweep.MergeToken(config.state_dir, lane_id(0)).acquire()

    def falls_over(self):
        raise RuntimeError("the filesystem fell over on the way out")

    monkeypatch.setattr(merge_sweep.MergeToken, "release", falls_over)

    merge_sweep.release_merge_token(token)      # must not raise
    merge_sweep.release_merge_token(None)       # nor must the one-lane no-op
