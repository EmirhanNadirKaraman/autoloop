"""notes-04: the change-note resolver runs in BOTH merge directions.

The loop merges these documentation trackers two ways, and until now only one
of them consulted `note_merge`:

  * a task branch INTO the base branch — `auto_merge.AutoMerger._merge`, wired
    to the resolver since docs-01 and pinned by `test_docs_merge.py`;
  * the base branch's head INTO a task branch —
    `orchestrator._carry_reviewed_candidate_past`, which refreshes a REVIEWED
    candidate's recorded base after the head moved under it. This had no note
    handling at all, so any change-note collision refused the whole operation.

Measured 2026-08-23, hours after notes-03 widened WHICH files may be combined:
`blk-quota-01-002` parked `task_base_behind_head` because the head could not be
merged into quota-01's branch — it conflicted at `docs/SUMMARY.md` and
`docs/TESTS.md`. Both are in `NOTE_TRACKERS`. Both are exactly the append-at-
the-end shape the resolver exists to combine. It was never consulted, and an
11-file reviewed candidate that had passed validation was abandoned.

Every task appends a change note by construction, so two tasks in flight across
one merge collide in the trackers by DEFAULT rather than by accident — which is
why `task_base_behind_head` is this repository's most common blocker code.

conc-14 (2026-09-10) added the second half of the same story. Combining the
trackers is worth nothing on a round that ALSO conflicts somewhere else,
because one unresolvable path refuses the whole merge — and a task that adds a
test file bumps a hardcoded suite-size counter, so at `lanes = 2` there was
always somewhere else. Measured that day: the first automatic merge stranded
both other candidates on "conflicts at
autoloop/tests/test_prose_doc_selection.py, autoloop/tests/test_test_selection.py,
docs/SUMMARY.md, docs/TESTS.md" — two trackers this file already covers, held
hostage by two copies of one number. The counter now has one home
(`suite_size.py`) and one mechanical resolution (`resolve_counter_bump`): the
merged value is the base's plus both sides' deltas, and both sides'
classification comments are kept.

WHAT IS PINNED HERE, in the order the claim states it:
  * a head conflicting only in change-note sections merges into the task
    branch, and BOTH sides' notes survive;
  * two branches that each added a test file carry forward together: the
    counter lands at base+2, both classification comments survive, and the
    trackers combine in the SAME merge;
  * a bump nobody classified, a counter that went DOWN, and a ledger line that
    was rewritten rather than appended to are each refused — the number is
    hand-written so that a new test file gets classified, and a resolver that
    summed unexplained increments would automate that away;
  * the same merge conflicting in a tracker's PROSE still parks;
  * a merge conflicting in any source file still parks, with the existing
    message and the existing blocker code;
  * a reviewed candidate is never rebased, rebuilt or quarantined past a
    conflict — the operator still decides;
  * and the ORDERING, which is the half that is easy to get wrong: the
    incoming head's note lines must lead so the refreshed branch can still be
    merged back OUT afterwards (`test_a_refreshed_task_branch_still_merges_
    back_out_through_auto_merge`). Getting that backwards trades one blocker
    for a branch that can never merge again — the ctx-01 shape, repaired by
    hand on 2026-08-21.

Real git throughout, real worker repositories built the way production builds
them (`WorkerRepoManager.create`), self-contained helpers — this package's
convention, see `test_postcommit_primitives.py` for why they are duplicated
rather than imported. The base branch is `work`, matching `test_docs_merge.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoloop import note_merge
from autoloop.auto_merge import AutoMerger, MergeDeferralStore
from autoloop.git_gateway import GitGateway
from autoloop.note_merge import NOTES_MARKER
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task
from autoloop.worker_env import WorkerRepoManager, worker_env
from autoloop.worktask import TaskExecution, TaskExecutionStore

TASK = Task(id="t1", title="T", description="d")

#: This file's own literal of the resolver's scope, deliberately a second copy
#: (same reasoning as `test_docs_merge.py`'s): every test here is a statement
#: about THESE paths, and `test_the_refresh_covers_exactly_the_declared_
#: trackers` is where the two are required to agree.
TRACKERS = (
    "docs/COMMON_ERRORS.md",
    "docs/SECURITY.md",
    "docs/SUMMARY.md",
    "docs/TESTS.md",
)

MARKER_LINE = f"{NOTES_MARKER} append below, one line per note, at the END. -->"
PROSE_ROW = "| `main.py` | FastAPI app. |"
SEED_NOTE = "| 2026-08-17 | seed-00 | the note that was already there |"

#: The counter half of the same claim. One path, read from the resolver rather
#: than spelled a second time: `test_the_two_resolvable_sets_are_disjoint_and_
#: routed_apart` is where the literal below and that set are required to agree.
COUNTER_PATH = "autoloop/tests/suite_size.py"
COUNTER_PROSE = '"""How many test files this suite has — written by hand."""'
COUNTER_MARKER_LINE = f"{note_merge.COUNTER_MARKER} one line per new test file, then bump."
SEED_CLASSIFICATION = "#: 99 -> 100 when seed-00 added `test_seed.py`: it reads no document."


def classification(who: str) -> str:
    """One branch's classification of the file it added — the line the counter
    file exists to make somebody write."""
    return f"#: when {who} added `test_{who}.py`: it reads no document and spawns nothing."


def counter_seed(value: int, *added: str) -> str:
    """The shape the resolver requires: prose, ONE marker, a ledger of comment
    lines, and the counter assignment as the LAST line of the file."""
    return (
        f"{COUNTER_PROSE}\n\n{COUNTER_MARKER_LINE}\n{SEED_CLASSIFICATION}\n"
        + "".join(f"{line}\n" for line in added)
        + f"{note_merge.COUNTER_NAME} = {value}\n"
    )


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def try_git(cwd, *args) -> subprocess.CompletedProcess:
    """Unchecked — used where a NON-zero exit is the thing being staged."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def contains(cwd, tip, sha) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, tip], cwd=str(cwd), capture_output=True
    ).returncode == 0


def seed(title: str) -> str:
    """The shape every real tracker has: prose, then ONE marker, then a ledger
    whose last line is a note row."""
    return (
        f"# {title}\n\n| Path | Purpose |\n|---|---|\n{PROSE_ROW}\n\n"
        f"## Change notes\n\n{MARKER_LINE}\n\n| Date | Task | Note |\n|---|---|---|\n"
        f"{SEED_NOTE}\n"
    )


SEEDS = {rel: seed(rel.rsplit("/", 1)[-1]) for rel in TRACKERS}


def note_line(who: str) -> str:
    return f"| 2026-08-23 | {who} | what {who} changed |\n"


def write(root, rel: str, text: str) -> None:
    target = Path(root) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def read(root, rel: str) -> str:
    return (Path(root) / rel).read_text(encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """The primary checkout: four seeded trackers, one source file, one doc
    that is deliberately NOT a tracker."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "work")
    git(root, "config", "user.email", "t@e.com")
    git(root, "config", "user.name", "T")
    git(root, "config", "commit.gpgsign", "false")
    for rel, text in SEEDS.items():
        write(root, rel, text)
    write(root, "autoloop/thing.py", "TIMEOUT = 30\n")
    write(root, "docs/TODO.md", "# TODO\n\n- one\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    return root


class _FakeWorkerRepos:
    """Records what a re-base would have asked of it. Every test below asserts
    both lists stay EMPTY: a reviewed candidate is never rebuilt."""

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


def _orch(repo, tmp_path, execution, review_round=1):
    """An Orchestrator with only what `_rebase_execution_if_stale` touches —
    same construction as `test_rebase_stale_base.py`'s."""
    from autoloop.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._policy = PolicyEngine(PolicyConfig())
    orch._git = GitGateway(repo, orch._policy)
    orch._worker_repos = _FakeWorkerRepos(tmp_path / "workers")
    # Same reason as `test_rebase_stale_base.py`'s fixture: no loop-owned
    # observed checkout, so the carry-forward below fetches the head from the
    # primary checkout exactly as it did before esc-02.
    orch._observed = None
    orch._observed_git = None
    orch._observed_synced_sha = ""
    orch._merge_deferrals = MergeDeferralStore(tmp_path / "deferrals")
    orch._execution_store = TaskExecutionStore(tmp_path / "executions")
    orch._logged: list = []
    orch._log = lambda event, **kw: orch._logged.append((event, kw))
    orch._parked: list = []
    orch._to_needs_user = lambda msg, **kw: orch._parked.append((msg, kw))
    execution.review_round = review_round
    orch._execution_store.save(execution)
    return orch


def _worker(repo, tmp_path, base, files, *, task_id="t1"):
    """A real worker repo carrying one committed candidate. Returns
    `(WorkerRepo, candidate_sha)`.

    `git init` + a one-time local fetch, exactly as production does — that
    separateness is what makes the fetch half of the merge load-bearing.
    """
    manager = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    worker = manager.create(task_id, repo, base)
    git(worker.path, "config", "user.email", "worker@example.com")
    git(worker.path, "config", "user.name", "Worker")
    git(worker.path, "config", "commit.gpgsign", "false")
    for rel, text in files.items():
        write(worker.path, rel, text)
    git(worker.path, "add", "-A")
    git(worker.path, "commit", "-qm", "the reviewed candidate")
    return worker, git(worker.path, "rev-parse", "HEAD")


def _reviewed(worker, candidate, base, **kw):
    return TaskExecution(
        task_id="t1",
        task_branch=worker.branch,
        worktree_path=str(worker.path),
        task_base_sha=base,
        candidate_sha=candidate,
        **kw,
    )


def _move_head(repo, files):
    for rel, text in files.items():
        write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "mainline shipped something")
    return git(repo, "rev-parse", "HEAD")


def _recording(who, *, extra=None):
    """The edit EVERY task makes: one new line at the end of EVERY tracker."""
    files = {rel: SEEDS[rel] + note_line(who) for rel in TRACKERS}
    files.update(extra or {})
    return files


def _install_counter(repo, value: int) -> str:
    """Add a counter file to the primary checkout and return the new base sha.

    A separate commit rather than a wider `repo` fixture: every test above was
    written against a base with no counter in it, and they must keep measuring
    exactly what they measured.
    """
    write(repo, COUNTER_PATH, counter_seed(value))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "the suite size, written by hand")
    return git(repo, "rev-parse", "HEAD")


def _adding_a_test_file(who, value: int, *, extra=None):
    """What a task that ADDS A TEST FILE changes, which is the round this whole
    section is about: a note in every tracker, plus the counter and the line
    classifying the file it added."""
    files = _recording(who)
    files[COUNTER_PATH] = counter_seed(value, classification(who))
    files.update(extra or {})
    return files


def _refresh(repo, tmp_path, worker, candidate, old_base, head, **kw):
    """Run the production dispatch step and return `(orch, execution, result)`."""
    execution = _reviewed(worker, candidate, old_base, **kw)
    orch = _orch(repo, tmp_path, execution, review_round=kw.get("review_round", 1))
    result = orch._rebase_execution_if_stale(execution, TASK)
    return orch, execution, result


def assert_nothing_was_rebased(orch, worker, execution, old_base, candidate):
    """The guard this task must not remove: a reviewed candidate is never
    rebased, rebuilt or quarantined, and the record is not re-pointed."""
    assert orch._worker_repos.created == [], "no worker may be rebuilt"
    assert orch._worker_repos.quarantined == [], "and none quarantined"
    assert execution.task_base_sha == old_base, "nothing was re-pointed"
    assert TaskExecutionStore(orch._execution_store.directory).load("t1").task_base_sha == old_base
    assert git(worker.path, "rev-parse", "HEAD") == candidate, "the branch tip is unmoved"
    assert git(worker.path, "status", "--porcelain") == "", "and the merge was aborted"


def assert_parked_the_same_way(orch, old_base, head):
    """The park is unchanged: same code, same three operator choices."""
    assert orch._parked, "it must park"
    message, kw = orch._parked[0]
    assert kw["code"] == "task_base_behind_head", "the same code, so the same recovery"
    assert kw["kind"] == "task_fatal"
    assert head[:12] in kw["detail"] and old_base[:12] in kw["detail"]
    assert "Either publish or abandon that candidate" in message
    assert "archive" in message
    return message


# --- the claim ----------------------------------------------------------------


def test_a_head_conflicting_only_in_change_notes_is_merged_into_the_task_branch(
    repo, tmp_path
):
    """THE provable claim. Two tasks in flight across one merge: each appended
    its own note to every tracker, so the head and the task branch collide in
    all four. The refresh succeeds and BOTH sides' notes survive."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    orch, _execution, result = _refresh(
        repo, tmp_path, worker, candidate, old_base, head,
        review_round=2, attempt_count=3, fault_attempt_count=1,
    )

    assert result is not None, "the dispatch must CONTINUE, not park"
    assert orch._parked == []
    assert result.task_base_sha == head
    # Nothing a moving base may touch was touched.
    assert result.candidate_sha == candidate
    assert result.review_round == 2 and result.attempt_count == 3
    assert result.fault_attempt_count == 1
    reloaded = TaskExecutionStore(tmp_path / "executions").load("t1")
    assert reloaded.task_base_sha == head and reloaded.candidate_sha == candidate

    tip = git(worker.path, "rev-parse", "HEAD")
    assert contains(worker.path, tip, candidate), "the reviewed object is still reachable"
    assert git(worker.path, "cat-file", "-t", candidate) == "commit"
    assert contains(worker.path, tip, head), "with the new head integrated"
    assert git(worker.path, "status", "--porcelain") == "", "and the worker is clean"
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []

    for rel in TRACKERS:
        text = read(worker.path, rel)
        assert "<<<<<<<" not in text, rel
        assert text.count(note_line("task-b")) == 1, rel
        assert text.count(note_line("mainline")) == 1, rel
        assert text.count(SEED_NOTE) == 1, rel
        assert text.count(MARKER_LINE) == 1, rel
        assert text.count(PROSE_ROW) == 1, rel


def test_a_refreshed_task_branch_still_merges_back_out_through_auto_merge(repo, tmp_path):
    """The half that is easy to get wrong, and the reason `lead` exists.

    `resolve_note_append` requires each side's section to hold the merge base's
    section as a literal PREFIX. After a refresh the incoming head IS the task's
    new base, so the head's note lines must come FIRST and the task's own must
    stay last — otherwise the branch's section no longer starts with its base's
    and EVERY later merge of that tracker refuses. That is the ctx-01 shape,
    which had to be repaired by hand on 2026-08-21.

    So this goes the whole way round: refresh the base, let mainline record one
    more note, then merge the task branch back out through the REAL
    `AutoMerger._resolve_note_conflicts` and require all three notes to survive.
    """
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)
    assert result is not None and orch._parked == []
    tip = git(worker.path, "rev-parse", "HEAD")

    # Mainline records one more note, so the merge back out really conflicts.
    later = {rel: SEEDS[rel] + note_line("mainline") + note_line("mainline-2")
             for rel in TRACKERS}
    _move_head(repo, later)

    # ... and now the ORIGINAL direction, through the code that already ships.
    git(repo, "fetch", "-q", str(worker.path), tip)
    merged = try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "merge task t1", tip)
    assert merged.returncode != 0, "the fixture must actually conflict, or this proves nothing"

    primary = GitGateway(repo, PolicyEngine(PolicyConfig()))
    merger = AutoMerger.__new__(AutoMerger)
    merger._git = primary
    merger._log = lambda event, **kw: None
    conflicts = primary.conflicted_paths()
    assert set(conflicts) == set(TRACKERS), conflicts

    assert merger._resolve_note_conflicts("t1", tip, conflicts, "merge task t1") is True, (
        "the refreshed branch must still be mergeable — if this fails, the "
        "refresh ordered the notes so that the branch can never merge again"
    )
    for rel in TRACKERS:
        text = read(repo, rel)
        assert "<<<<<<<" not in text, rel
        for who in ("task-b", "mainline", "mainline-2"):
            assert text.count(note_line(who)) == 1, f"{who} in {rel}"
        assert text.count(SEED_NOTE) == 1, rel


def test_the_incoming_notes_lead_and_the_tasks_own_notes_stay_last(repo, tmp_path):
    """The ordering stated directly, so a regression names itself rather than
    surfacing as a mysterious refusal one merge later."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    _refresh(repo, tmp_path, worker, candidate, old_base, head)

    for rel in TRACKERS:
        text = read(worker.path, rel)
        assert text.index(note_line("mainline")) < text.index(note_line("task-b")), rel
        assert text.endswith(note_line("task-b")), rel
        # And stated as the invariant the next merge actually checks: the
        # branch's section is the incoming base's section plus its own lines.
        assert text == read(repo, rel) + note_line("task-b"), rel


def test_the_wrong_ordering_really_would_break_the_next_merge():
    """The counter-case, at the unit level. Without this the test above could
    pass for reasons unrelated to `lead`, and the bound would be unproven.

    A branch whose section is `base + own + incoming` does NOT start with the
    incoming base's section, so the resolver refuses — forever."""
    base = seed("Tracker")
    incoming = base + note_line("mainline")

    wrong_way = base + note_line("task-b") + note_line("mainline")
    right_way = base + note_line("mainline") + note_line("task-b")

    later = incoming + note_line("mainline-2")
    assert note_merge.resolve_note_append(incoming, later, wrong_way, incoming) is None
    assert note_merge.resolve_note_append(incoming, later, right_way, incoming) is not None


# --- the counter every task that adds a test file bumps (conc-14) -------------


def test_two_branches_each_adding_a_test_file_carry_forward_together(repo, tmp_path):
    """THE claim conc-14 states. Two tasks in flight, each having added a test
    file: they collide in all four trackers AND in the counter, which is the
    combination that stranded every candidate at `lanes = 2`.

    The whole set resolves in ONE merge: the counter lands at base+2 — 116 plus
    one new file from each side — and both sides' classification comments are
    kept, so the number is still explained file by file.
    """
    old_base = _install_counter(repo, 116)
    worker, candidate = _worker(repo, tmp_path, old_base, _adding_a_test_file("task-b", 117))
    head = _move_head(repo, _adding_a_test_file("mainline", 117))

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is not None, "the dispatch must CONTINUE, not park"
    assert orch._parked == []
    assert result.task_base_sha == head and result.candidate_sha == candidate

    text = read(worker.path, COUNTER_PATH)
    assert "<<<<<<<" not in text, text
    assert text.endswith(f"{note_merge.COUNTER_NAME} = 118\n"), text
    assert text.count(classification("mainline")) == 1, "the head's file is still classified"
    assert text.count(classification("task-b")) == 1, "and so is the task's"
    assert text.count(SEED_CLASSIFICATION) == 1, "and the ledger it started from"
    assert text.count(note_merge.COUNTER_MARKER) == 1
    assert text.index(classification("mainline")) < text.index(classification("task-b")), (
        "the incoming head's block leads, for the reason `lead` exists"
    )
    # The trackers combined in the SAME merge, which is the point: before this
    # they were refused BECAUSE the counter could not be resolved beside them.
    for rel in TRACKERS:
        tracker = read(worker.path, rel)
        assert tracker.count(note_line("task-b")) == 1, rel
        assert tracker.count(note_line("mainline")) == 1, rel
    resolved = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_resolved"]
    assert resolved and resolved[0]["paths"] == sorted((COUNTER_PATH, *TRACKERS))
    body = git(worker.path, "log", "-1", "--format=%B")
    assert f"Both sides' counter bumps combined automatically in {COUNTER_PATH}." in body


def test_a_carried_forward_counter_still_merges_back_out_through_auto_merge(repo, tmp_path):
    """The round trip, for the reason `test_a_refreshed_task_branch_still_
    merges_back_out_through_auto_merge` exists: an ordering that looks right in
    the worker but leaves the branch unmergeable is the expensive failure, and
    the ledger has exactly the prefix requirement the trackers have.

    So: carry forward (116 -> 118), let mainline add one more test file of its
    own (117 -> 118 on ITS side), then merge the task branch back out through
    the real `AutoMerger._resolve_note_conflicts`. 119, and all three
    classification blocks."""
    old_base = _install_counter(repo, 116)
    worker, candidate = _worker(repo, tmp_path, old_base, _adding_a_test_file("task-b", 117))
    head = _move_head(repo, _adding_a_test_file("mainline", 117))

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)
    assert result is not None and orch._parked == []
    tip = git(worker.path, "rev-parse", "HEAD")

    later = {rel: SEEDS[rel] + note_line("mainline") + note_line("mainline-2")
             for rel in TRACKERS}
    later[COUNTER_PATH] = counter_seed(
        118, classification("mainline"), classification("mainline-2")
    )
    _move_head(repo, later)

    git(repo, "fetch", "-q", str(worker.path), tip)
    merged = try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "merge task t1", tip)
    assert merged.returncode != 0, "the fixture must actually conflict, or this proves nothing"

    primary = GitGateway(repo, PolicyEngine(PolicyConfig()))
    merger = AutoMerger.__new__(AutoMerger)
    merger._git = primary
    merger._log = lambda event, **kw: None
    conflicts = primary.conflicted_paths()
    assert set(conflicts) == {COUNTER_PATH, *TRACKERS}, conflicts

    assert merger._resolve_note_conflicts("t1", tip, conflicts, "merge task t1") is True, (
        "the refreshed branch must still be mergeable — if this fails, the "
        "refresh ordered the ledger so that the branch can never merge again"
    )
    text = read(repo, COUNTER_PATH)
    assert "<<<<<<<" not in text, text
    assert text.endswith(f"{note_merge.COUNTER_NAME} = 119\n"), text
    for who in ("task-b", "mainline", "mainline-2"):
        assert text.count(classification(who)) == 1, who


def test_a_source_conflict_alongside_a_resolvable_counter_resolves_nothing(repo, tmp_path):
    """The bound conc-14 must not move, in the shape it exists for. Everything
    that CAN be combined is here — four trackers and the counter — and one
    source file genuinely conflicts. One conflicted path nothing can resolve
    still refuses the WHOLE merge and parks; nothing is written."""
    old_base = _install_counter(repo, 116)
    worker, candidate = _worker(
        repo, tmp_path, old_base,
        _adding_a_test_file("task-b", 117, extra={"autoloop/thing.py": "TIMEOUT = 60\n"}),
    )
    head = _move_head(
        repo, _adding_a_test_file("mainline", 117, extra={"autoloop/thing.py": "TIMEOUT = 90\n"})
    )

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    assert read(worker.path, COUNTER_PATH) == counter_seed(117, classification("task-b")), (
        "a resolvable counter must never reach disk when a source file refused"
    )
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused, "the resolver must say why it declined"
    assert "outside" in refused[0]["reason"]
    assert "autoloop/thing.py" in refused[0]["reason"]


def test_a_counter_whose_ledger_was_rewritten_still_parks(repo, tmp_path):
    """The counter is not a licence to merge that FILE, only that SHAPE. Two
    branches rewriting the classification line that was already there is a real
    content conflict and needs a human, exactly like a rewritten note."""
    old_base = _install_counter(repo, 116)
    rewritten = SEED_CLASSIFICATION.replace("seed-00", "somebody else")
    worker, candidate = _worker(
        repo, tmp_path, old_base,
        {COUNTER_PATH: counter_seed(116).replace(SEED_CLASSIFICATION, rewritten + " (task)")},
    )
    head = _move_head(
        repo,
        {COUNTER_PATH: counter_seed(116).replace(SEED_CLASSIFICATION, rewritten + " (mainline)")},
    )

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert f"conflicts at {COUNTER_PATH}" in message
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused and "not two branches bumping the same counter" in refused[0]["reason"]


def test_a_trackers_only_refresh_says_exactly_what_it_always_said(repo, tmp_path):
    """`lanes = 1`, where nothing adds a test file concurrently: the trackers
    are the only conflict, and both the outcome and the WORDING are the ones
    that shipped before conc-14 — the counter sentence appears only when a
    counter was actually combined."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is not None and orch._parked == []
    body = git(worker.path, "log", "-1", "--format=%B")
    assert body.endswith(
        "Append-only change notes combined automatically in " + ", ".join(TRACKERS) + "."
    ), body
    assert "counter" not in body


# --- the counter rule itself, at the unit level -------------------------------


def test_the_counter_resolver_adds_both_deltas_and_keeps_both_classifications():
    """The arithmetic, stated directly: base plus one new file from each side.

    Both orders, because `lead` decides which side's block leads and the same
    ordering rule applies here as in the trackers."""
    base = counter_seed(116)
    ours = counter_seed(117, classification("mainline"))
    theirs = counter_seed(117, classification("task-b"))

    assert note_merge.resolve_counter_bump(base, ours, theirs, base) == counter_seed(
        118, classification("mainline"), classification("task-b")
    )
    assert note_merge.resolve_counter_bump(
        base, ours, theirs, base, lead=note_merge.THEIRS_FIRST
    ) == counter_seed(118, classification("task-b"), classification("mainline"))


def test_a_side_that_added_two_test_files_is_added_up_as_two():
    """The delta is a COUNT, not a flag: a side that bumped by two contributes
    two, which is what makes "base plus the files both sides added" true rather
    than "base plus one each"."""
    base = counter_seed(116)
    ours = counter_seed(118, classification("mainline"), classification("mainline-2"))
    theirs = counter_seed(117, classification("task-b"))

    combined = note_merge.resolve_counter_bump(base, ours, theirs, base)

    assert combined == counter_seed(
        119, classification("mainline"), classification("mainline-2"), classification("task-b")
    )


def test_a_bump_nobody_classified_is_refused_rather_than_summed():
    """The check the hardcoded number exists to force. A side that raised the
    count without saying what it added is refused — otherwise the resolver
    would automate away the classification one merge at a time, and the
    constant would become an arithmetic result nobody had read."""
    base = counter_seed(116)
    silent = counter_seed(117)
    classified = counter_seed(117, classification("task-b"))

    assert note_merge.resolve_counter_bump(base, silent, classified, base) is None
    assert note_merge.resolve_counter_bump(base, classified, silent, base) is None
    assert note_merge.resolve_counter_bump(base, classified, classified, base) is not None


def test_a_counter_that_went_down_or_sideways_is_refused():
    """A DECREASE is not a bump and is not something to add up: it is a file
    deleted, or a hand edit, and either way the merged number is not
    arithmetic. Refusing parks, which is the safe direction."""
    base = counter_seed(116)
    lower = counter_seed(115, classification("mainline"))
    higher = counter_seed(117, classification("task-b"))

    assert note_merge.resolve_counter_bump(base, lower, higher, base) is None
    assert note_merge.resolve_counter_bump(base, higher, lower, base) is None


def test_the_counter_resolver_refuses_everything_that_is_not_this_shape():
    """One case per way the file can stop being a ledger with a counter at the
    end. Each of these leaves the conflict standing, so the caller parks.
    """
    base = counter_seed(116)
    ours = counter_seed(117, classification("mainline"))
    theirs = counter_seed(117, classification("task-b"))
    assert note_merge.resolve_counter_bump(base, ours, theirs, base) is not None, (
        "the control: without this every assertion below could pass for the "
        "wrong reason"
    )

    # A ledger line that was already there, rewritten rather than appended to.
    edited = counter_seed(117, classification("task-b")).replace(SEED_CLASSIFICATION, "#: no.")
    assert note_merge.resolve_counter_bump(base, ours, edited, base) is None
    # Code appended into the ledger, so the region is no longer only comments.
    smuggled = counter_seed(117, "import os")
    assert note_merge.resolve_counter_bump(base, ours, smuggled, base) is None
    # The counter is no longer the last line of the file.
    trailing = counter_seed(117, classification("task-b")) + "\nEXTRA = 1\n"
    assert note_merge.resolve_counter_bump(base, ours, trailing, base) is None
    # No final newline: the "append" continued the counter line itself.
    assert note_merge.resolve_counter_bump(base, ours, theirs.rstrip("\n"), base) is None
    # No marker at all, and two of them — the same count check either way.
    assert note_merge.resolve_counter_bump(
        base, ours, theirs.replace(COUNTER_MARKER_LINE, "# nothing"), base
    ) is None
    assert note_merge.resolve_counter_bump(
        base, ours, theirs.replace(COUNTER_MARKER_LINE, COUNTER_MARKER_LINE + "\n" + COUNTER_MARKER_LINE), base
    ) is None
    # Neither side changed anything the resolver can see.
    assert note_merge.resolve_counter_bump(base, base, base, base) is None
    # An unrecognised `lead` raises rather than defaulting, for the reason
    # `resolve_note_append` gives: the wrong order is an unmergeable branch.
    with pytest.raises(ValueError):
        note_merge.resolve_counter_bump(base, ours, theirs, base, lead="whatever")


def test_the_counter_is_never_read_from_the_half_merged_working_file():
    """THE echo check. `merged` is git's own conflicted file, and the one place
    the answer is NOT: git wrote markers over exactly the counter line. It is
    used for the region above the marker and nothing else — and a conflict in
    THAT region refuses, because it is the module's prose disagreeing."""
    base = counter_seed(116)
    ours = counter_seed(117, classification("mainline"))
    theirs = counter_seed(117, classification("task-b"))

    # A working file whose counter line says something absurd changes nothing:
    # the value comes from the index stages.
    noisy = counter_seed(999, "#: <<<<<<< not read from here")
    assert note_merge.resolve_counter_bump(base, ours, theirs, noisy) == counter_seed(
        118, classification("mainline"), classification("task-b")
    )
    # But a conflict ABOVE the marker is git saying the prose disagrees.
    conflicted_head = base.replace(COUNTER_PROSE, "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> theirs")
    assert note_merge.resolve_counter_bump(base, ours, theirs, conflicted_head) is None


def test_identical_bumps_are_not_counted_twice():
    """Both branches carrying the same new file — one cherry-picked onto the
    other. Git resolves that itself and never reaches the resolver, but adding
    the deltas would count one file twice and duplicate its classification."""
    base = counter_seed(116)
    same = counter_seed(117, classification("task-b"))

    assert note_merge.resolve_counter_bump(base, same, same, base) == same


def test_the_two_resolvable_sets_are_disjoint_and_routed_apart():
    """A path in both lists would be resolved by whichever branch was checked
    first, which is a coin toss between two different rules."""
    assert not (note_merge.NOTE_TRACKERS & note_merge.COUNTER_FILES)
    assert note_merge.COUNTER_FILES == frozenset({COUNTER_PATH})
    assert note_merge.resolver_for(COUNTER_PATH) is note_merge.resolve_counter_bump
    for rel in TRACKERS:
        assert note_merge.resolver_for(rel) is note_merge.resolve_note_append
    for outside in ("autoloop/thing.py", "CLAUDE.md", "docs/SCHEMA.md", "autoloop/tests/"):
        assert note_merge.resolver_for(outside) is None


# The two claims about the SHIPPED counter file — that it has the shape the
# resolver needs, and that the constant has exactly one home — live in
# `test_docs_merge.py` instead of here, and the reason is this file's own
# subject matter. Both need `Path(__file__)` to reach the checkout, and this
# module names all four trackers in evaluated code (`TRACKERS` above); the two
# together are exactly `validation._files_reading_documents`' definition of a
# READER, so adding a repo root here would put this file into every docs-only
# round and move the counts `test_prose_doc_selection.py` and
# `test_test_selection.py` pin. `test_docs_merge.py` is already a reader of all
# four, so the same tests cost nothing there.


# --- what must still park -----------------------------------------------------


@pytest.mark.parametrize("rel", TRACKERS)
def test_a_conflict_in_tracker_prose_still_parks(repo, tmp_path, rel):
    """A conflict ABOVE the marker is ordinary documentation disagreeing and
    needs a human. Run per tracker: a passing result on one says nothing about
    the file added yesterday."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(
        repo, tmp_path, old_base,
        {rel: SEEDS[rel].replace(PROSE_ROW, "| `main.py` | the task's words. |")},
    )
    head = _move_head(
        repo, {rel: SEEDS[rel].replace(PROSE_ROW, "| `main.py` | mainline's words. |")}
    )

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert f"conflicts at {rel}" in message
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    assert not [e for e, _ in orch._logged if e == "execution_base_notes_resolved"]
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused and refused[0]["reason"], "the refusal must explain itself"


def test_a_conflict_in_a_source_file_still_parks_with_the_existing_message(repo, tmp_path):
    """The bound on the whole change: only append-only change notes are in
    scope. A genuine source conflict parks with the message it always had."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, {"autoloop/thing.py": "TIMEOUT = 60\n"})
    head = _move_head(repo, {"autoloop/thing.py": "TIMEOUT = 90\n"})

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert "conflicts at autoloop/thing.py" in message, "it names what actually clashed"
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    assert read(worker.path, "autoloop/thing.py") == "TIMEOUT = 60\n", "no marker was left"
    assert [e for e, _ in orch._logged if e == "execution_base_carry_forward_refused"]


def test_a_source_conflict_alongside_resolvable_trackers_resolves_nothing(repo, tmp_path):
    """The case the trackers cannot buy their way out of — and the shape that
    actually occurred. All four trackers are clean pairs of appends that WOULD
    combine; one source file genuinely conflicts. One conflicted path outside
    the list refuses the WHOLE merge, and no tracker is written."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(
        repo, tmp_path, old_base, _recording("task-b", extra={"autoloop/thing.py": "TIMEOUT = 60\n"})
    )
    head = _move_head(
        repo, _recording("mainline", extra={"autoloop/thing.py": "TIMEOUT = 90\n"})
    )

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    for rel in TRACKERS:
        text = read(worker.path, rel)
        assert note_line("mainline") not in text, (
            f"a resolvable {rel} must never reach disk when a source file refused"
        )
        assert text.count(note_line("task-b")) == 1, rel
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused, "the resolver must say why it declined"
    assert "outside" in refused[0]["reason"]
    assert "autoloop/thing.py" in refused[0]["reason"]
    assert refused[0]["conflicted_files"] == sorted(("autoloop/thing.py", *TRACKERS))


def test_a_documentation_file_outside_the_trackers_still_parks(repo, tmp_path):
    """What "narrow" buys: the same append-at-EOF edit that combines in the
    trackers still parks in a doc nobody granted the resolver. `docs/` is not a
    prefix and never becomes one."""
    old_base = git(repo, "rev-parse", "HEAD")
    todo = "# TODO\n\n- one\n"
    worker, candidate = _worker(repo, tmp_path, old_base, {"docs/TODO.md": todo + "- from the task\n"})
    head = _move_head(repo, {"docs/TODO.md": todo + "- from mainline\n"})

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused and refused[0]["conflicted_files"] == ["docs/TODO.md"]
    assert "outside" in refused[0]["reason"]


def test_an_edited_existing_note_line_still_parks(repo, tmp_path):
    """A note already in the ledger is not append-only content — it is a claim
    somebody made. Rewriting it is a real content conflict, in this direction
    exactly as in the other."""
    old_base = git(repo, "rev-parse", "HEAD")
    rel = "docs/SUMMARY.md"
    worker, candidate = _worker(
        repo, tmp_path, old_base,
        {rel: SEEDS[rel].replace(SEED_NOTE, "| 2026-08-17 | seed-00 | the task's rewording |")},
    )
    head = _move_head(
        repo,
        {rel: SEEDS[rel].replace(SEED_NOTE, "| 2026-08-17 | seed-00 | mainline's rewording |")},
    )

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused and "not two branches appending change notes" in refused[0]["reason"]


@pytest.mark.parametrize("rel", TRACKERS)
def test_a_tracker_without_the_marker_is_refused_rather_than_combined(repo, tmp_path, rel):
    """The precondition behind the list, proven in this direction too. A
    granted path whose file has no append-only section gives the resolver no
    boundary between prose and ledger — so it refuses, and the failure mode is
    a park, never a combined paragraph."""
    unmarked = "# Tracker\n\nJust prose, no append-only section at all.\n"
    write(repo, rel, unmarked)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a tracker with no section")
    old_base = git(repo, "rev-parse", "HEAD")

    worker, candidate = _worker(repo, tmp_path, old_base, {rel: unmarked + "a line from the task\n"})
    head = _move_head(repo, {rel: unmarked + "a line from mainline\n"})

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    assert read(worker.path, rel) == unmarked + "a line from the task\n"


# --- the reviewed-candidate guard, unchanged ----------------------------------


def test_a_dirty_worker_is_not_merged_over_even_when_only_notes_conflict(repo, tmp_path):
    """Residue in a worker is an interrupted round's work or a failed round's
    evidence. The precondition runs BEFORE any merge is attempted, so making
    note conflicts resolvable must not let a resolvable one slip past it."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))
    (worker.path / "half-written.txt").write_text("mid-round\n", encoding="utf-8")

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert "uncommitted changes" in message
    assert execution.task_base_sha == old_base
    assert git(worker.path, "rev-parse", "HEAD") == candidate
    assert (worker.path / "half-written.txt").read_text() == "mid-round\n"
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []
    assert not [e for e, _ in orch._logged if e.startswith("execution_base_notes_")]


def test_a_branch_tip_that_lost_the_candidate_is_not_merged_into(repo, tmp_path):
    """The approval binding stays a CHECKED fact. A resolvable note conflict is
    no reason to skip it."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))
    git(worker.path, "reset", "-q", "--hard", old_base)

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert "does not contain the reviewed candidate" in message
    assert git(worker.path, "rev-parse", "HEAD") == old_base, "and nothing was merged"
    assert not [e for e, _ in orch._logged if e.startswith("execution_base_notes_")]


def test_the_resolution_is_named_in_the_transcript(repo, tmp_path):
    """A merge the loop resolved without a human is exactly the thing an
    operator must be able to find afterwards."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is not None
    resolved = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["paths"] == list(TRACKERS)
    assert resolved[0]["task_id"] == "t1"
    assert resolved[0]["head"] == head and resolved[0]["candidate_sha"] == candidate
    assert not [e for e, _ in orch._logged if e == "execution_base_notes_refused"]
    # And the automatic resolution is visible in git, not only in the log.
    assert "combined automatically" in git(worker.path, "log", "-1", "--format=%B")


def test_the_refresh_covers_exactly_the_declared_trackers():
    """This file's literal and the resolver's list must agree, or every test
    above is quietly scoped to something else."""
    assert note_merge.NOTE_TRACKERS == frozenset(TRACKERS)
    for excluded in ("CLAUDE.md", "docs/SCHEMA.md", "docs/", "docs/TODO.md"):
        assert excluded not in note_merge.NOTE_TRACKERS


# --- the direction that already worked, and the hook's own guards -------------


def test_the_task_to_mainline_direction_is_unchanged():
    """`lead` defaults to the order that shipped, so every existing caller —
    `auto_merge`, and `test_docs_merge.py`'s positional calls — behaves exactly
    as before."""
    base = seed("Tracker")
    ours = base + note_line("mainline")
    theirs = base + note_line("task-b")

    assert note_merge.resolve_note_append(base, ours, theirs, base) == (
        base + note_line("mainline") + note_line("task-b")
    )
    assert note_merge.resolve_note_append(base, ours, theirs, base, lead=note_merge.OURS_FIRST) == (
        note_merge.resolve_note_append(base, ours, theirs, base)
    )


def test_an_unknown_lead_is_refused_rather_than_defaulted():
    """Silently falling back to `OURS_FIRST` in the refresh direction is the
    unmergeable-branch bug arriving without a word, so a typo raises."""
    base = seed("Tracker")
    with pytest.raises(ValueError):
        note_merge.resolve_note_append(base, base, base, base, lead="whatever")
    with pytest.raises(ValueError):
        note_merge.combine_conflicted_notes(None, ["docs/TESTS.md"], "m", lead="whatever")


def test_no_conflicted_path_is_refused_rather_than_read_as_resolved(tmp_path):
    """An unreadable `git status` reports NO conflicted path. Treating that as
    "everything resolved" is the fail-open shape: the resolver would conclude a
    merge it never looked at."""
    outcome = note_merge.combine_conflicted_notes(None, [], "m")
    assert outcome.resolved is False
    assert outcome.refusal and outcome.paths == ()


def _plain_gateway(path):
    """A gateway rooted at `path` under the SCRUBBED worker environment, the
    way production builds one. Not decoration: without it these subprocesses
    resolve the developer's ambient git config, and an `insteadOf` rule or a
    signing requirement in it would decide the result instead of the code."""
    return GitGateway(path, PolicyEngine(PolicyConfig()), env=worker_env())


def _plain_pair(tmp_path, name):
    """Two repos where merging the second's commit into the first conflicts in
    a source file — the gateway-level fixture, no orchestrator involved."""
    src = tmp_path / f"{name}-src"
    src.mkdir()
    git(src, "init", "-q", "-b", "work")
    git(src, "config", "user.email", "t@e.com")
    git(src, "config", "user.name", "T")
    git(src, "config", "commit.gpgsign", "false")
    write(src, "f.txt", "one\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "first")
    base = git(src, "rev-parse", "HEAD")

    dst = tmp_path / f"{name}-dst"
    subprocess.run(["git", "clone", "-q", str(src), str(dst)], check=True)
    git(dst, "config", "user.email", "t@e.com")
    git(dst, "config", "user.name", "T")
    git(dst, "config", "commit.gpgsign", "false")
    write(dst, "f.txt", "the local line\n")
    git(dst, "add", "-A")
    git(dst, "commit", "-qm", "local")
    local_tip = git(dst, "rev-parse", "HEAD")

    write(src, "f.txt", "the incoming line\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "incoming")
    return src, dst, base, local_tip, git(src, "rev-parse", "HEAD")


def test_a_resolver_that_claims_success_without_committing_is_not_believed(tmp_path):
    """A hook returning True is a CLAIM. The gateway applies the same
    discipline to it that it applies to `git merge` returning 0 — and a hook
    that concluded nothing leaves the merge exactly where git did, so the
    ordinary abort still runs and the tree comes back clean."""
    src, dst, _base, local_tip, incoming = _plain_pair(tmp_path, "liar")
    gw = _plain_gateway(dst)

    attempt = gw.merge_foreign_commit(
        str(src), incoming, "merge", resolve_conflicts=lambda g, c: True
    )

    assert attempt.merged is False, "an unearned True must not become a merge"
    assert attempt.conflicted_paths == ("f.txt",)
    assert attempt.restored is True
    assert git(dst, "rev-parse", "HEAD") == local_tip, "the branch is unmoved"
    assert git(dst, "status", "--porcelain") == ""
    assert read(dst, "f.txt") == "the local line\n"


def test_a_resolution_that_committed_the_wrong_thing_is_reported_as_a_failure(tmp_path):
    """The other half of "a True is a CLAIM": a hook that DID commit, but
    committed something that does not contain the commit being merged in.

    Only a resolver bug can reach this, which is exactly why it is driven here
    rather than left as an unexercised verification branch. Two things are
    pinned: it is not accepted, and it reports NO conflicted paths — the paths
    conflicted, but their conflict is not what went wrong, and naming them would
    route the caller's park at the path list and hide `error` entirely.
    """
    src, dst, _base, local_tip, incoming = _plain_pair(tmp_path, "wrongcommit")
    gw = _plain_gateway(dst)

    def abort_and_commit_something_else(g, c):
        git(dst, "merge", "--abort")
        write(dst, "unrelated.txt", "not the merge at all\n")
        git(dst, "add", "-A")
        git(dst, "commit", "-qm", "an unrelated commit")
        return True

    attempt = gw.merge_foreign_commit(
        str(src), incoming, "merge", resolve_conflicts=abort_and_commit_something_else
    )

    assert attempt.merged is False, "an unverifiable resolution is not a merge"
    assert attempt.conflicted_paths == (), "this failure was not a content conflict"
    assert "does not contain the merged commit" in attempt.error
    assert attempt.restored is True
    assert contains(dst, git(dst, "rev-parse", "HEAD"), local_tip), "nothing was discarded"
    assert not contains(dst, git(dst, "rev-parse", "HEAD"), incoming)


def test_an_unverifiable_resolution_parks_naming_the_real_condition(repo, tmp_path):
    """The same failure through the production dispatch, because the routing is
    the point. `_carry_reviewed_candidate_past` prefers the conflicted-path list
    when there is one, so an unverifiable resolution reported WITH paths would
    park as "it conflicts at docs/SUMMARY.md" and never show the operator that
    the loop wrote a merge commit it could not vouch for."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    def lying_hook(g, conflicts):
        git(worker.path, "merge", "--abort")
        write(worker.path, "unrelated.txt", "not the merge at all\n")
        git(worker.path, "add", "-A")
        git(worker.path, "commit", "-qm", "an unrelated commit")
        return True

    execution = _reviewed(worker, candidate, old_base, review_round=1)
    orch = _orch(repo, tmp_path, execution, review_round=1)
    orch._note_conflict_resolver = lambda *a, **k: lying_hook

    result = orch._rebase_execution_if_stale(execution, TASK)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert "git refused:" in message, "the park must not claim the trackers conflicted"
    assert "does not contain the merged commit" in message
    assert "conflicts at" not in message
    assert execution.task_base_sha == old_base, "nothing was re-pointed"
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []
    assert contains(worker.path, git(worker.path, "rev-parse", "HEAD"), candidate), (
        "the reviewed object is still reachable — this path discards nothing, "
        "it stops and asks"
    )


def test_a_resolver_that_raises_falls_through_to_the_abort(tmp_path):
    """A resolver that blew up has resolved nothing. It must not crash the
    dispatch and must not become a success."""
    from autoloop.errors import GitCommandError

    src, dst, _base, local_tip, incoming = _plain_pair(tmp_path, "raiser")
    gw = _plain_gateway(dst)

    def boom(g, c):
        raise GitCommandError("the index could not be read")

    attempt = gw.merge_foreign_commit(str(src), incoming, "merge", resolve_conflicts=boom)

    assert attempt.merged is False and attempt.restored is True
    assert attempt.conflicted_paths == ("f.txt",)
    assert git(dst, "rev-parse", "HEAD") == local_tip
    assert git(dst, "status", "--porcelain") == ""


def test_a_failure_that_is_not_a_conflict_never_reaches_the_hook(tmp_path):
    """The fail-open case from the other side. A failure with no unmerged path
    — here an unfetchable object — must not reach a resolver at all: handed an
    empty list it could only no-op its way to declaring the merge concluded.
    `test_no_conflicted_path_is_refused_rather_than_read_as_resolved` pins the
    same refusal one layer down, in the resolver itself."""
    src, dst, _base, local_tip, _incoming = _plain_pair(tmp_path, "unfetchable")
    gw = _plain_gateway(dst)
    calls: list = []

    attempt = gw.merge_foreign_commit(
        str(src), "0" * 40, "merge", resolve_conflicts=lambda g, c: calls.append(c) or True
    )

    assert attempt.merged is False
    assert calls == [], "a fetch failure never reaches the resolver"
    assert git(dst, "rev-parse", "HEAD") == local_tip


def test_omitting_the_hook_behaves_exactly_as_before(tmp_path):
    """Every caller that does not pass one — and there is one such caller in
    the tree — gets the pre-existing behaviour, conflict for conflict."""
    src, dst, _base, local_tip, incoming = _plain_pair(tmp_path, "default")
    gw = _plain_gateway(dst)

    attempt = gw.merge_foreign_commit(str(src), incoming, "merge")

    assert attempt.merged is False
    assert attempt.conflicted_paths == ("f.txt",)
    assert attempt.restored is True
    assert git(dst, "rev-parse", "HEAD") == local_tip
