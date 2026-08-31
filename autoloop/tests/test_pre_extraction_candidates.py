"""A completed task whose `candidate_sha` the 2026-08-27 extraction rewrote.

`git filter-repo --path autoloop/` rewrote every commit and migrated no
execution record, so 133 completed tasks name a commit that is not in this
object database at all. Every one reported `origin/refs/heads/autoloop/<id> does
not exist` and held the merge sweep for 90 hours. Pushing the 133 refs was tried
on 2026-08-30 and reverted — the refs carry the REWRITTEN commits, so the answer
becomes `is at <new>, not the candidate` — and an ancestor walk from each branch
tip put 8 branches on ANOTHER task's commit the same day.

What answers is `docs/extraction/commit-map.tsv`, filter-repo's own old->new
map, vendored as evidence. This file pins the rules that read it, and pins just
as hard the states in which they must resolve NOTHING: no map, an ambiguous
abbreviation, an unwalkable history, a task id in a commit body, a merge subject
naming a neighbouring id.

Real git throughout, over repositories built with
`gitrepo.make_repo_from_template` — the CLAIM is about git ancestry and about
commits that do or do not exist, which is the one thing a fixture cannot model.
Nothing here may be MERGED: every sweep in this file runs over a merger that
records what it was asked for, and every test asserts it was asked for nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoloop import auto_merge, merge_sweep
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.git_gateway import GitGateway
from autoloop.merge_sweep import BacklogSweeper
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import TaskExecution, TaskExecutionStore

from gitrepo import make_repo_from_template, run_git

URL = "https://chatgpt.com/c/merge-09"
BASE = "work"

#: Pre-extraction shas: 40 hex characters that are not objects in any fixture
#: repository here, which is exactly what the 133 records name.
OLD_LANDED = "953a3265" + "a" * 32
OLD_PRUNED = "01218a92" + "b" * 32
OLD_ABSENT = "deadbeef" + "c" * 32
OLD_DISCARDED = "0ddba11c" + "d" * 32


# --- fixtures -----------------------------------------------------------------


class RecordingMerger:
    """Stands in for `AutoMerger` and records what it was asked to integrate.

    Returns `MERGED` rather than raising, deliberately: `BacklogSweeper._attempt`
    catches every exception and turns it into a stopped sweep, so an assertion
    thrown from here would be swallowed and the test would read a plausible
    outcome. The assertion belongs in the test, over `attempted`.
    """

    def __init__(self):
        self.attempted = []

    def attempt(self, task_id, seen=None):
        self.attempted.append(task_id)
        return auto_merge.MERGED


class Fixture:
    """A checkout, a registry, an execution store, and whatever pre-extraction
    wreckage a test asks for."""

    def __init__(self, tmp_path):
        self.root = make_repo_from_template(tmp_path / "repo", branch=BASE)
        self.config = AutoloopConfig(
            browser=BrowserConfig(conversation_url=URL),
            policy=PolicyConfig(auto_merge_enabled=True),
            state_dir=tmp_path / ".al",
            workers_root=tmp_path / "workers",
        )
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.executions = TaskExecutionStore(self.config.executions_dir)
        self.registry = TaskRegistry()
        # Saved as well as held: `cli._merge_window_blockers` re-reads the
        # registry from disk, and a completed task it cannot see there is not
        # exempted from the window it would otherwise hold shut.
        self.task_store = TaskStore(self.config.tasks_file)
        self.task_store.save(self.registry)
        self.merger = RecordingMerger()

    # -- building the history --

    def head(self) -> str:
        return run_git(self.root, "rev-parse", "HEAD").strip()

    def commit(self, message, **files) -> str:
        for rel, body in (files or {"note.txt": message}).items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        run_git(self.root, "add", "-A")
        run_git(self.root, "commit", "-q", "-m", message)
        return self.head()

    def side_commit(self, branch, message, **files) -> str:
        """One commit on its own branch, left UNMERGED — the shape of work the
        rewrite kept but nobody integrated."""
        run_git(self.root, "checkout", "-q", "-b", branch)
        sha = self.commit(message, **files)
        run_git(self.root, "checkout", "-q", BASE)
        return sha

    def merge_commit(self, subject, sha) -> str:
        """A real `--no-ff` merge onto the base, exactly as `AutoMerger._merge`
        makes one: two parents, and the subject it writes."""
        run_git(self.root, "merge", "--no-ff", "--no-edit", "-m", subject, sha)
        return self.head()

    def write_map(self, rows, *, text=None):
        """Vendor a commit map. `rows` is `[(old, new)]`; `text` writes the file
        verbatim instead, for the malformed-input cases."""
        path = self.root / merge_sweep.COMMIT_MAP_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            text = "old                                      new\n" + "".join(
                f"{old} {new}\n" for old, new in rows
            )
        path.write_text(text, encoding="utf-8")
        run_git(self.root, "add", "-A")
        run_git(self.root, "commit", "-q", "-m", "vendor the extraction map")
        return path

    # -- building the wreckage --

    def completed(self, task_id, candidate_sha, *, remote="origin", dest_ref=None):
        """A COMPLETED task whose live record names `candidate_sha`."""
        self.executions.save(
            TaskExecution(
                task_id=task_id,
                task_branch=f"autoloop/{task_id}",
                worktree_path="",
                task_base_sha="",
                candidate_sha=candidate_sha,
                review_round=1,
                intended_remote=remote,
                intended_remote_ref=dest_ref or f"refs/heads/autoloop/{task_id}",
            )
        )
        self.register(task_id)

    def register(self, task_id):
        self.registry.add_many(
            [Task(id=task_id, title=f"Title {task_id}", description="d")]
        )
        self.registry.mark_completed(task_id)
        self.task_store.save(self.registry)

    def archived(self, task_id, label, *, anonymous=False, **named):
        """One archived execution record under `label`.

        With no `candidate_sha`/`published_sha` it carries no candidate of its
        own and the sha is in the FILENAME and nowhere else, which is what the
        reconciler left behind.

        `anonymous` writes a record with no `task_id` inside it, which
        `_is_another_tasks_copy` KEEPS — "cannot tell" must not read as "not
        mine" — so a test can put the filename anchor under load on its own.
        """
        archive = self.config.executions_dir / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        path = archive / f"{task_id}-{label}.json"
        body = {"task_branch": f"autoloop/{task_id}", **named}
        if not anonymous:
            body["task_id"] = task_id
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    # -- running it --

    def sweep(self):
        policy = PolicyEngine(self.config.policy)
        return BacklogSweeper(
            config=self.config,
            git=GitGateway(self.root, policy),
            policy=policy,
            execution_store=self.executions,
            registry=self.registry,
            log=TranscriptLogger(self.config.transcript_file).append,
            merger=self.merger,
        ).sweep()

    # -- observing it --

    def entries(self, entry_type=None):
        path = self.config.transcript_file
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if entry_type is None or r["type"] == entry_type]

    def recovered(self):
        return [r["data"] for r in self.entries(merge_sweep.SWEEP_RECOVERED_EVENT)]

    def reason(self, task_id, result) -> str:
        return next(why for tid, why in result.unresolved if tid == task_id)


@pytest.fixture()
def fx(tmp_path):
    return Fixture(tmp_path)


# --- the map answers ----------------------------------------------------------


def test_a_mapped_candidate_that_IS_an_ancestor_resolves(fx):
    """The 118-task case. The record names a commit from the old history; the
    map says which commit the rewrite turned it into; that commit is an ancestor
    of the base head, so the branch is accounted for and nothing is outstanding.

    Judged EXACTLY as if the record had named the new sha — `merge-base
    --is-ancestor` and nothing else. No ref is consulted and no claim is read.
    """
    landed = fx.commit("work that survived the rewrite", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.completed("abort-01", OLD_LANDED)

    result = fx.sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert result.unresolved == []
    assert result.is_clear is True, "a candidate proved to be in the base is clear"
    assert fx.merger.attempted == [], "nothing here may be merged"
    [entry] = fx.recovered()
    assert entry["task_id"] == "abort-01"
    assert entry["verdict"] == merge_sweep.CANDIDATE_IN_BASE
    assert entry["rule"] == merge_sweep.RULE_COMMIT_MAP
    assert entry["sha"] == landed


def test_a_mapped_candidate_that_is_NOT_an_ancestor_stays_unresolved(fx):
    """The map ANSWERING is not the same as the answer being good news. A row
    pointing at a real commit that is not in the base means the work may still
    be outstanding, and that is `unresolved` — the sweep cannot merge it either,
    because the record names an object nothing here can merge from."""
    outstanding = fx.side_commit("autoloop/stray-01", "never integrated")
    fx.write_map([(OLD_LANDED, outstanding)])
    fx.completed("stray-01", OLD_LANDED)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["stray-01"]
    assert result.is_clear is False
    assert fx.merger.attempted == []
    assert fx.recovered() == [], "an outstanding branch is not a resolved one"
    why = fx.reason("stray-01", result)
    assert outstanding[:12] in why and "not an ancestor" in why


def test_a_NULL_sha_row_is_nothing_to_merge_and_not_a_merged_claim(fx):
    """Forty zeros means filter-repo PRUNED the commit: it touched no path under
    `autoloop/`, so nothing of it is in this repository. There is nothing to
    merge AND nothing that should exist — which is a different verdict from
    `in-base`, and is recorded as one. Calling it merged would claim work landed
    that this repository has never contained."""
    fx.write_map([(OLD_PRUNED, merge_sweep.NULL_SHA)])
    fx.completed("pruned-01", OLD_PRUNED)

    result = fx.sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert result.unresolved == []
    assert result.is_clear is True
    assert fx.merger.attempted == []
    [entry] = fx.recovered()
    assert entry["verdict"] == merge_sweep.CANDIDATE_PRUNED
    assert entry["verdict"] != merge_sweep.CANDIDATE_IN_BASE
    assert "PRUNED" in entry["detail"]


def test_a_candidate_ABSENT_from_the_map_stays_unresolved(fx):
    """No fallback and no guessing. A row that is not there is not evidence of
    anything, and the task stays exactly where it was before these rules
    existed."""
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("orphan-01", OLD_ABSENT)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["orphan-01"]
    assert result.is_clear is False
    assert fx.merger.attempted == []
    assert fx.recovered() == []
    assert "is not in docs/extraction/commit-map.tsv" in fx.reason("orphan-01", result)


def test_a_map_that_is_NOT_THERE_resolves_nothing(fx):
    """The fail-open this section is most exposed to: the evidence file is the
    guard, and a guard that switches itself off when its input is missing never
    fires and says nothing. An absent map must clear NOTHING — not the pruned
    reading, not the merged one."""
    fx.commit("a commit the map would have named", **{"autoloop/a.py": "one\n"})
    fx.completed("abort-01", OLD_LANDED)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["abort-01"]
    assert result.is_clear is False
    assert fx.merger.attempted == []
    assert fx.recovered() == []
    assert "could not be read" in fx.reason("abort-01", result)


def test_an_EMPTY_map_is_not_a_map_that_says_nothing_is_mapped(fx):
    """A file that exists and holds no usable row is the same "could not look"
    as no file at all. Reading it as "this commit is not in the map" would turn
    a truncated copy into a verdict about 133 tasks."""
    fx.write_map([], text="old                                      new\n")
    fx.completed("abort-01", OLD_LANDED)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["abort-01"]
    assert "no usable old->new rows" in fx.reason("abort-01", result)
    assert fx.recovered() == []


# --- the archived filename ----------------------------------------------------


def test_an_empty_re_dispatched_record_is_answered_by_the_ARCHIVED_filename(fx):
    """dash-02, pkt-02, pkt-03 and audit-0001 exactly: the live record is an
    empty stub left by a re-dispatch, and the only thing on disk naming the
    commit the completion was recorded over is the archive LABEL —
    `<id>-reconciled-as-<sha>-<stamp>.json`. The record inside names nothing."""
    landed = fx.commit("the work that was reconciled", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.completed("dash-02", "")
    fx.archived("dash-02", f"reconciled-as-{OLD_LANDED[:7]}-20260810T000000Z")

    result = fx.sweep()

    assert result.unresolved == []
    assert result.is_clear is True
    assert fx.merger.attempted == []
    [entry] = fx.recovered()
    assert entry["task_id"] == "dash-02"
    assert entry["verdict"] == merge_sweep.CANDIDATE_IN_BASE
    assert entry["sha"] == landed


def test_a_merged_as_label_answers_the_same_way(fx):
    """The other label the operator's own hand-merges left behind."""
    landed = fx.commit("hand-merged work", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.completed("pkt-02", "")
    fx.archived("pkt-02", f"merged-as-{OLD_LANDED[:8]}-20260810T000000Z")

    result = fx.sweep()

    assert result.unresolved == []
    assert [e["verdict"] for e in fx.recovered()] == [merge_sweep.CANDIDATE_IN_BASE]


def test_an_empty_record_with_NOTHING_naming_a_candidate_stays_unresolved(fx):
    """The rule that was already here, unchanged: completion means a candidate
    was published, so a record naming none — with no archive naming one either —
    is a branch this module cannot name. The wording every reader of these
    reasons greps for is kept."""
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("blank-01", "")

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["blank-01"]
    assert "names no candidate" in fx.reason("blank-01", result)
    assert fx.recovered() == []


def test_an_archived_label_from_a_SIBLING_id_cannot_answer(fx):
    """The archive glob matches by filename PREFIX and task ids contain `-`, so
    `rt-1-*.json` finds `rt-1-b-reconciled-as-<sha>-<stamp>.json`. A sibling's
    retirement must not become this task's newest generation and answer for work
    it says nothing about."""
    landed = fx.commit("rt-1-b's work", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.completed("rt-1", "")
    fx.archived("rt-1-b", f"reconciled-as-{OLD_LANDED[:7]}-20260810T000000Z")

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["rt-1"]
    assert fx.recovered() == []


def test_a_sibling_copy_that_cannot_be_ATTRIBUTED_still_cannot_answer(fx):
    """The same hazard with the cheaper guard removed. A copy naming no owner is
    KEPT — "cannot tell" must not read as "not mine" — so the only thing
    stopping `rt-1-b`'s retirement from answering for `rt-1` is that the label
    is anchored on the task's own id, in `_filename_sha`."""
    landed = fx.commit("rt-1-b's work", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.completed("rt-1", "")
    fx.archived(
        "rt-1-b",
        f"reconciled-as-{OLD_LANDED[:7]}-20260810T000000Z",
        anonymous=True,
    )

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["rt-1"]
    assert fx.recovered() == []
    assert "names no sha in its filename" in fx.reason("rt-1", result)


def test_only_the_NEWEST_archived_generation_answers(fx):
    """One archived file is one retirement, and several describe DIFFERENT
    commits. The newest is the one the completion was recorded over; an older
    one is very often integrated precisely because it was superseded."""
    superseded = fx.commit("the abandoned attempt", **{"autoloop/a.py": "one\n"})
    outstanding = fx.side_commit("autoloop/late", "the retry, never merged")
    fx.write_map([(OLD_LANDED, superseded), (OLD_DISCARDED, outstanding)])
    fx.completed("retry-01", "")
    fx.archived("retry-01", f"reconciled-as-{OLD_LANDED[:7]}-20260810T000000Z")
    fx.archived("retry-01", f"reconciled-as-{OLD_DISCARDED[:7]}-20260812T000000Z")

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["retry-01"], (
        "the newest retirement names work that is still outstanding"
    )
    assert outstanding[:12] in fx.reason("retry-01", result)


def test_a_NEWER_archive_naming_no_sha_does_not_hide_an_older_one_that_does(fx):
    """`audit-0001`, the last of the 133 (2026-08-31). Two archived copies: an
    older `-reconciled-as-<sha>-<stamp>` naming the commit its completion was
    recorded over, and a NEWER `-report-recovered-by-operator-<stamp>` naming
    none. Reading only the newest found nothing and gave up, and the task stayed
    unjudgeable for want of a file the rule does not even apply to.

    A label carrying no sha is not evidence that no archived label names the
    commit — the scan passes over it and asks the generation below. Here that
    yields a sha the map records as PRUNED, which is the real audit-0001
    answer: its only files were `docs/AUDIT_*.md` at the SOURCE repo root, never
    under `autoloop/`.
    """
    fx.write_map([(OLD_PRUNED, merge_sweep.NULL_SHA)])
    fx.completed("audit-0001", "")
    fx.archived("audit-0001", f"reconciled-as-{OLD_PRUNED[:7]}-20260817T230000Z")
    fx.archived("audit-0001", "report-recovered-by-operator-20260825T120000Z")

    result = fx.sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert result.unresolved == []
    assert result.is_clear is True
    assert fx.merger.attempted == [], "nothing here may be merged"
    [entry] = fx.recovered()
    assert entry["task_id"] == "audit-0001"
    assert entry["verdict"] == merge_sweep.CANDIDATE_PRUNED
    assert entry["sha"] == merge_sweep.NULL_SHA
    assert "reconciled-as" in entry["detail"], "the copy that answered is named"
    assert "report-recovered" not in entry["detail"], (
        "the newest copy named no sha and did not answer; saying it did would be "
        "a false statement in the operator's transcript"
    )


def test_the_first_MATCHING_label_answers_even_when_an_OLDER_one_would_resolve(fx):
    """The fail-open the scan itself could become. It stops at the first label
    the pattern applies to — NOT at the first sha that happens to resolve — so a
    superseded reconciliation cannot clear a task whose latest one names work
    that is still outstanding. Walking on past an unwelcome answer would be a
    search for good news rather than for evidence."""
    landed = fx.commit("the abandoned attempt", **{"autoloop/a.py": "one\n"})
    outstanding = fx.side_commit("autoloop/late", "the retry, never merged")
    fx.write_map([(OLD_LANDED, landed), (OLD_DISCARDED, outstanding)])
    fx.completed("scan-01", "")
    fx.archived("scan-01", f"reconciled-as-{OLD_LANDED[:7]}-20260810T000000Z")
    fx.archived("scan-01", f"reconciled-as-{OLD_DISCARDED[:7]}-20260812T000000Z")
    fx.archived("scan-01", "report-recovered-by-operator-20260814T000000Z")

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["scan-01"]
    assert fx.recovered() == []
    why = fx.reason("scan-01", result)
    assert outstanding[:12] in why, "the newest label NAMING a sha is the answer"
    assert landed[:12] not in why, "an older, superseded label may not answer"


def test_a_TIE_in_which_only_ONE_copy_names_a_sha_is_refused_not_passed_over(fx):
    """Passing over a label the pattern does not cover is a rule about ORDER —
    an older generation answering when a newer one is silent. Inside a single
    generation there is no order: two retirements written in the same second
    cannot be told apart, so which of them describes the retirement is exactly
    what is unknown. Refused, like a tie naming two different shas."""
    landed = fx.commit("work", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.completed("tie-01", "")
    fx.archived("tie-01", f"reconciled-as-{OLD_LANDED[:7]}-20260812T000000Z")
    fx.archived("tie-01", "report-recovered-by-operator-20260812T000000Z")

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["tie-01"]
    assert fx.recovered() == []
    assert "do not agree on one sha" in fx.reason("tie-01", result)


def test_a_DIGIT_RUN_in_a_label_the_pattern_does_not_cover_is_not_a_sha(fx):
    """`-report-recovered-by-operator-20260825-` carries a dash-delimited run of
    hex-shaped characters, and a pattern loosened to "a sha-shaped substring
    somewhere in the filename" would read it as one. The map here has a row
    keyed by exactly that run, so a loosened pattern would CLEAR this task; the
    anchored one answers nothing at all."""
    landed = fx.commit("work a loose pattern would claim", **{"autoloop/a.py": "1\n"})
    fx.write_map([("20260825" + "e" * 32, landed)])
    fx.completed("loose-01", "")
    fx.archived("loose-01", "report-recovered-by-operator-20260825-20260825T230000Z")

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["loose-01"]
    assert fx.recovered() == []
    assert fx.merger.attempted == []
    assert "names no sha in its filename" in fx.reason("loose-01", result)


def test_an_UNORDERABLE_archive_is_still_refused_when_the_newest_names_no_sha(fx):
    """Passing over a label the pattern does not cover must not leak into
    passing over one that cannot be ORDERED. Dropping the unstamped copy and
    scanning what is left would answer here — and would be answering from a
    generation that may not be the newest, which is the whole reason an
    unorderable archive is refused."""
    landed = fx.commit("work", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.completed("ord-01", "")
    fx.archived("ord-01", f"reconciled-as-{OLD_LANDED[:7]}-20260810T000000Z")
    fx.archived("ord-01", "report-recovered-by-operator")

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["ord-01"]
    assert fx.recovered() == []
    assert "cannot be put in order" in fx.reason("ord-01", result)


def test_archived_copies_that_cannot_be_ORDERED_answer_nothing(fx):
    """Two generations and one unstamped label: which is newest cannot be told,
    and an unorderable archive is refused rather than guessed at."""
    landed = fx.commit("work", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.completed("murky-01", "")
    fx.archived("murky-01", f"reconciled-as-{OLD_LANDED[:7]}-20260810T000000Z")
    fx.archived("murky-01", f"reconciled-as-{OLD_LANDED[:7]}")

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["murky-01"]
    assert "cannot be put in order" in fx.reason("murky-01", result)
    assert fx.recovered() == []


def test_a_RETIRED_record_naming_a_pre_extraction_candidate_is_recovered_too(fx):
    """The rewrite renamed the commit an ARCHIVED record names just as
    thoroughly as the one a live record names, and a completed task whose record
    was retired reaches the base through `execution_record_ancestry` — which
    asks git about a sha that is not in this object database.

    The recovery is the sweep's own, injected beside the ancestry callable, so
    `dashboard.registry_disagreements` keeps getting the answer it always got
    from the shared function.
    """
    landed = fx.commit("work that survived the rewrite", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed)])
    fx.register("retired-01")
    fx.archived("retired-01", "published-20260810T000000Z", candidate_sha=OLD_LANDED)

    result = fx.sweep()

    assert result.unresolved == []
    assert result.is_clear is True
    assert fx.merger.attempted == []
    [entry] = fx.recovered()
    assert entry["rule"] == merge_sweep.RULE_COMMIT_MAP
    assert entry["sha"] == landed


def test_a_retired_record_naming_TWO_shas_is_left_to_the_stricter_rule(fx):
    """`_record_verdict` requires EVERY sha a tied generation names to be
    integrated, because two retirements inside one second cannot be ordered.
    Recovering on any-one-of would be a weaker rule wearing the same name, so
    more than one sha refuses outright."""
    landed = fx.commit("one of the two", **{"autoloop/a.py": "one\n"})
    fx.write_map([(OLD_LANDED, landed), (OLD_PRUNED, merge_sweep.NULL_SHA)])
    fx.register("retired-02")
    fx.archived(
        "retired-02",
        "published-20260810T000000Z",
        candidate_sha=OLD_LANDED,
        published_sha=OLD_PRUNED,
    )

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["retired-02"]
    assert fx.recovered() == []


# --- the merge commit ---------------------------------------------------------


def test_a_DISCARDED_candidate_is_resolved_by_the_merge_commit_naming_it(fx):
    """brw-18: the operator discarded the candidate the record names, so the map
    has no row for it — but mainline carries the loop's own merge commit, whose
    subject is `Merge task <id> (<sha>)`. Everything on the first-parent chain
    of the base head is an ancestor of it by construction, so finding it there
    IS the ancestry answer."""
    work = fx.side_commit("autoloop/brw-18", "the work", **{"autoloop/a.py": "one\n"})
    fx.merge_commit(f"Merge task brw-18 ({work[:12]}) into {BASE}", work)
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("brw-18", OLD_DISCARDED)

    result = fx.sweep()

    assert result.unresolved == []
    assert result.is_clear is True
    assert fx.merger.attempted == []
    [entry] = fx.recovered()
    assert entry["task_id"] == "brw-18"
    assert entry["rule"] == merge_sweep.RULE_MERGE_COMMIT
    assert entry["verdict"] == merge_sweep.CANDIDATE_IN_BASE


def test_a_merge_subject_naming_a_LONGER_id_never_answers(fx):
    """`brw-180` is a different, equally valid task id. The id is parsed out of
    the subject and compared by equality, so a substring can never match — the
    ancestor-walk mistake of 2026-08-30 was exactly this class of near-miss."""
    work = fx.side_commit("autoloop/brw-180", "another task's work")
    fx.merge_commit(f"Merge task brw-180 ({work[:12]}) into {BASE}", work)
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("brw-18", OLD_DISCARDED)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["brw-18"]
    assert fx.recovered() == []
    assert "Merge task brw-18 (<sha>)" in fx.reason("brw-18", result)


def test_a_task_id_in_a_commit_BODY_clears_nothing(fx):
    """A written claim is not evidence. Only the SUBJECT of a merge commit is
    read, and only in the one form the merger writes — a body mentioning the
    task, however plausibly, resolves nothing."""
    work = fx.side_commit("autoloop/other", "someone else's work")
    fx.merge_commit(
        "Merge task other-01 (0123456789ab) into work\n\n"
        "This also carries the work for brw-18, honestly it does.",
        work,
    )
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("brw-18", OLD_DISCARDED)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["brw-18"]
    assert fx.recovered() == []


def test_an_ORDINARY_commit_shaped_like_a_merge_subject_clears_nothing(fx):
    """The loop's integrations are always `--no-ff` merges, so a commit with one
    parent is not one however its subject reads. Requiring two parents is what
    keeps a hand-written subject from being a clearance."""
    fx.commit(f"Merge task brw-18 ({'a' * 12}) into {BASE}")
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("brw-18", OLD_DISCARDED)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["brw-18"]
    assert fx.recovered() == []


def test_a_merge_commit_the_base_head_does_NOT_reach_clears_nothing(fx):
    """The walk starts at the base head and follows first parents, so a merge
    made on a branch nobody merged back is invisible to it — which is the
    conservative direction: its work is not in the base either."""
    work = fx.side_commit("autoloop/brw-18", "the work")
    run_git(fx.root, "checkout", "-q", "-b", "somewhere-else")
    fx.merge_commit(f"Merge task brw-18 ({work[:12]}) into somewhere-else", work)
    run_git(fx.root, "checkout", "-q", BASE)
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("brw-18", OLD_DISCARDED)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["brw-18"]
    assert fx.recovered() == []


def test_a_history_that_cannot_be_WALKED_clears_nothing(fx, monkeypatch):
    """An incomplete search is not a search that found nothing. A walk that git
    would not answer for, or that ran past its cap, comes back as `None` and
    every task relying on it stays unresolved."""
    work = fx.side_commit("autoloop/brw-18", "the work")
    fx.merge_commit(f"Merge task brw-18 ({work[:12]}) into {BASE}", work)
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("brw-18", OLD_DISCARDED)
    monkeypatch.setattr(merge_sweep, "FIRST_PARENT_WALK_CAP", 0)

    result = fx.sweep()

    assert [tid for tid, _why in result.unresolved] == ["brw-18"]
    assert fx.recovered() == []
    assert "could not be walked" in fx.reason("brw-18", result)


# --- what must NOT change -----------------------------------------------------


def test_a_candidate_this_checkout_HOLDS_is_judged_exactly_as_before(fx):
    """The gate on the whole section. A record naming a real local commit that
    is not in the base goes down the path it always took — the remote is asked,
    and the branch is attempted — with no rule here looking at it.

    The fail-open this pins is specific and was designed against: a task merged
    once and RE-DISPATCHED carries `Merge task <id>` in the base while its new
    branch is genuinely outstanding. Reading that old merge as an answer about
    the new branch would silently skip a branch the sweep exists to merge.
    """
    first = fx.side_commit("autoloop/again-01", "the first round")
    fx.merge_commit(f"Merge task again-01 ({first[:12]}) into {BASE}", first)
    retry = fx.side_commit("autoloop/again-01-retry", "the second round")
    origin = fx.root.parent / "origin.git"
    run_git(fx.root, "init", "-q", "--bare", str(origin))
    run_git(fx.root, "remote", "add", "origin", str(origin))
    run_git(fx.root, "push", "-q", "origin", f"{retry}:refs/heads/autoloop/again-01")
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("again-01", retry)

    result = fx.sweep()

    assert fx.merger.attempted == ["again-01"], (
        "an outstanding branch confirmed on the remote is still swept"
    )
    assert result.merged == ["again-01"]
    assert result.unresolved == []
    assert fx.recovered() == [], "no rule here may look at a candidate git can resolve"


def test_a_candidate_the_checkout_lacks_but_the_REMOTE_confirms_is_still_swept(fx):
    """The ordinary production shape, and the one most easily broken here: the
    candidate was pushed from a worker repository, so the main checkout has
    never fetched it. `AutoMerger._ensure_object` handles that, and the map
    having no row for it must leave the branch on exactly that path."""
    other = make_repo_from_template(fx.root.parent / "worker", branch=BASE)
    (other / "a.py").write_text("one\n", encoding="utf-8")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-q", "-m", "work in a worker repo")
    candidate = run_git(other, "rev-parse", "HEAD").strip()
    origin = fx.root.parent / "origin.git"
    run_git(fx.root, "init", "-q", "--bare", str(origin))
    run_git(other, "remote", "add", "origin", str(origin))
    run_git(other, "push", "-q", "origin", f"{candidate}:refs/heads/autoloop/far-01")
    run_git(fx.root, "remote", "add", "origin", str(origin))
    fx.write_map([(OLD_LANDED, fx.head())])
    fx.completed("far-01", candidate)

    result = fx.sweep()

    assert fx.merger.attempted == ["far-01"]
    assert result.unresolved == []
    assert fx.recovered() == [], "the map has no row for a post-extraction sha"


# --- the map file itself ------------------------------------------------------


def test_the_header_and_every_malformed_row_are_dropped():
    """Both columns must be 40 hex characters. That drops filter-repo's own
    `old new` header with no special case, and drops a truncated or reflowed
    line rather than reading half of it as a sha."""
    rows = merge_sweep.parse_commit_map(
        "old                                      new\n"
        f"{OLD_LANDED} {'1' * 40}\n"
        f"{OLD_PRUNED} {'2' * 39}\n"
        f"{OLD_ABSENT[:39]} {'3' * 40}\n"
        "\n"
        f"{OLD_DISCARDED} {'4' * 40} extra\n"
    )

    assert rows == {OLD_LANDED: "1" * 40}


def test_a_row_that_CONTRADICTS_itself_is_dropped_rather_than_chosen_between():
    """The file is evidence. A duplicated key with two different answers is a
    contradiction, and picking the first or the last would be this module
    guessing at which half of its evidence to trust."""
    rows = merge_sweep.parse_commit_map(
        f"{OLD_LANDED} {'1' * 40}\n"
        f"{OLD_LANDED} {'2' * 40}\n"
        f"{OLD_PRUNED} {'3' * 40}\n"
        f"{OLD_PRUNED} {'3' * 40}\n"
    )

    assert rows == {OLD_PRUNED: "3" * 40}, (
        "a duplicate that agrees with itself has nothing to disagree about"
    )


def test_an_AMBIGUOUS_abbreviation_answers_nothing():
    """An archived filename carries a 7-character sha, so the map is queried by
    prefix — and a prefix matching two rows names neither."""
    commit_map = merge_sweep.CommitMap(
        rows={"abc1234" + "0" * 33: "1" * 40, "abc1234" + "5" * 33: "2" * 40}
    )

    new, why_not = commit_map.lookup("abc1234")

    assert new == ""
    assert "cannot be told" in why_not


def test_an_abbreviation_SHORTER_than_git_would_print_is_refused():
    """Uniqueness is the real guard, but a two-character prefix that happens to
    be unique in a small map is an accident rather than evidence."""
    commit_map = merge_sweep.CommitMap(rows={"ab" + "0" * 38: "1" * 40})

    new, why_not = commit_map.lookup("ab")

    assert new == ""
    assert "at least 7" in why_not


def test_a_non_hex_key_is_refused_rather_than_matched():
    """A path, a branch name or an empty string is not a sha, and `startswith`
    would happily compare one."""
    commit_map = merge_sweep.CommitMap(rows={"abc1234" + "0" * 33: "1" * 40})

    assert commit_map.lookup("refs/heads/x")[0] == ""
    assert commit_map.lookup("")[0] == ""
    assert commit_map.lookup("ABC1234" + "0" * 33)[0] == "1" * 40, (
        "an upper-case sha is the same sha"
    )


def test_a_checkout_that_will_not_name_its_ROOT_gets_no_map():
    """The map is evidence about ONE repository. A gateway that cannot say which
    one it is must not fall back to the process's working directory and read
    some other checkout's copy — it gets an error, and every candidate it would
    have answered for stays unresolved."""
    commit_map = merge_sweep.load_commit_map(None)

    assert commit_map.rows == {}
    assert "repository root" in commit_map.error


def test_the_vendored_map_is_present_and_parses():
    """The evidence is TRACKED, which is the only reason a worker repository can
    reach it — `.git/` is never cloned, so an agent asked to reconcile
    pre-extraction work could not otherwise see filter-repo's map at all."""
    root = Path(__file__).resolve().parents[2]
    commit_map = merge_sweep.load_commit_map(root)

    assert commit_map.error == ""
    assert len(commit_map.rows) == 623, "624 lines: one header and 623 rows"
    pruned = [old for old, new in commit_map.rows.items() if new == merge_sweep.NULL_SHA]
    assert len(pruned) == 206, "rows filter-repo pruned to forty zeros"


def test_every_verdict_is_one_the_sweep_knows_how_to_handle():
    """`CANDIDATE_VERDICTS` is the closed set; `_backlog` handles the resolved
    two by skipping, `outstanding` by naming, and `unrecovered` by leaving the
    task on the path it was already on."""
    assert set(merge_sweep.CANDIDATE_RESOLVED) <= set(merge_sweep.CANDIDATE_VERDICTS)
    assert set(merge_sweep.CANDIDATE_VERDICTS) == {
        merge_sweep.CANDIDATE_IN_BASE,
        merge_sweep.CANDIDATE_PRUNED,
        merge_sweep.CANDIDATE_OUTSTANDING,
        merge_sweep.CANDIDATE_UNRECOVERED,
    }
