"""ctx-07: a completed task leaves the project context true, or files the ONE
narrow task that will — and never widens its own scope to do either.

THE CLAIM, and what each section pins of it: at completion the loop classifies
the published change against the context records THAT ROUND'S PACKET SELECTED,
updates only the records whose own files fall inside the task's own
`approved_paths`, and for everything else files ONE narrow follow-up task
through the inbox that depends on the completed task.

§1 is the record FILE — writing one, reading it back, and superseding without
rewriting. §2 is the four questions, as a pure classification over stated
records: it builds no repository, because "does this record's path appear in
this set of changed paths" is a claim about two sets and a real commit would be
dead weight. §3 is question four's bar, off the loop's own attempt ledger.
§4 files the follow-up through the real inbox gate and the real registry, since
a request that shape-checks in a test and is refused on drain is a follow-up
nobody ever reads. §5 and §6 need real git: "the records the packet SELECTED" is
a claim about bytes a round was actually given, and §6 drives the whole push
path that grades it. §7 is the scope rule, asserted twice — once on what the
registry holds afterwards, once on what this code path can even reach.

§8 is ctx-16: the store that machinery had nowhere to point at. It lives here
rather than beside it because the claim is about the same push path §6 already
builds — a store IS wired, the closeout RUNS against it, and the tree the escape
detector watches is byte-clean afterwards because the records in it are read and
never written. §8.3 is the provenance half: the records are read out of git AT
THE ROUND'S BASE, so a packet that says `task_base_sha: B1` quotes B1's record
bytes even after the observed branch has moved to B2.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from gitrepo import make_repo_from_template, run_git

from autoloop.config import AutoloopConfig, BrowserConfig, ContextConfig
from autoloop.context_index import build_index, load_index
from autoloop.context_packet import (
    FOLLOW_UP_SUFFIX,
    LESSON_MIN_OCCURRENCES,
    CloseoutPlan,
    ContextPacketStore,
    classify_closeout,
    follow_up_id_for,
    follow_up_request,
    plan_round_closeout,
    render_context_packet,
    repeated_failure,
    selection_was_shown,
)
from autoloop.context_records import (
    ContextRecord,
    ContextRecordError,
    ContextRecordStore,
    load_records,
    record_from_mapping,
    record_to_mapping,
    repository_record_store,
    superseded_record,
)
from autoloop.context_resolver import (
    DANGLING_SUPERSESSION,
    STALENESS_UNKNOWN,
    SUPERSEDED,
    SelectedRecord,
    resolve_context,
)
from autoloop.contract import Decision, Directive
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.inbox import TaskInbox, apply_requests, check_request_shape
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LastResponse, LoopState, StateStore
from autoloop.tasks import TRACKER_PATHS, Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import (
    ATTEMPT_FAULT,
    ATTEMPT_PENDING,
    ATTEMPT_TASK,
    REASON_SENT_FOR_REVIEW,
    IntentStore,
    TaskExecution,
    TaskExecutionStore,
    format_attempt,
)
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/test-conversation"

MAX_RECORDS = 25

#: The published commit every pure test verifies against. A full sha, because
#: that is what `TaskExecution.published_sha` holds.
PUBLISHED = "b" * 40

#: Where a record file is called from, in the tests that use a store.
PREFIX = "docs/context"


def record(record_id="feat", kind="feature", **kwargs) -> ContextRecord:
    """A record complete for every kind (`REQUIRED_FIELDS`) and VERIFIED AT NO
    COMMIT. Since ctx-14 the loader resolves a record's `last_verified_commit`
    through the worker's gateway and refuses one it cannot find, so a fake sha
    here would make every record this file writes to a store unreadable on the
    round that loads it. An empty commit cites nothing, loads, and is exactly
    the "never verified" record the closeout advances — a test that wants a
    commit on the record says which."""
    fields = {
        "title": "feature.py greets exactly once",
        "invariant": "feature.py greets exactly once",
        "source_paths": ("feature.py",),
        "last_verified_commit": "",
    }
    fields.update(kwargs)
    return ContextRecord(id=record_id, kind=kind, **fields)


def chosen(*records: ContextRecord) -> tuple[SelectedRecord, ...]:
    """`resolve_context`'s output shape, stated rather than resolved — §2 is
    about the classification, and a resolution there would be a second thing
    that could fail."""
    return tuple(
        SelectedRecord(record=item, reason="seed", depth=0, staleness=STALENESS_UNKNOWN)
        for item in records
    )


def sources_of(*records: ContextRecord) -> dict[str, str]:
    return {item.id: f"{item.id}.json" for item in records}


def store_at(tmp_path, prefix=PREFIX, name="records") -> ContextRecordStore:
    return ContextRecordStore(tmp_path / name, prefix)


def unit(paths=("feature.py", "docs/context/"), cite=("feat",), task_id="t1") -> Task:
    return Task(
        id=task_id,
        title=f"Title {task_id}",
        description="desc",
        approved_paths=tuple(paths),
        context_ids=tuple(cite),
    )


def plan_for(
    tmp_path,
    *records: ContextRecord,
    task=None,
    changed=("feature.py",),
    published=PUBLISHED,
    ledger=(),
    store=None,
):
    store = store or store_at(tmp_path)
    return classify_closeout(
        task or unit(),
        chosen(*records),
        store,
        changed_paths=frozenset(changed),
        published_sha=published,
        sources=sources_of(*records),
        known_record_ids=frozenset(item.id for item in records),
        known_filenames=frozenset(f"{item.id}.json" for item in records),
        attempt_ledger=ledger,
    )


# =============================================================================
# 1. THE RECORD FILE — written, read back, and superseded rather than rewritten
# =============================================================================


def test_a_record_round_trips_through_the_mapping_it_is_written_as():
    """The write format is the read format. A field this dropped would be a
    field an update silently deleted from every record it touched.

    `empty` carries the one field a lesson must (`REQUIRED_FIELDS`, ctx-14) and
    nothing else, so every OTHER field round-trips from its empty value."""
    full = record(
        related_ids=("other",), superseded_by="successor", last_verified_commit="c" * 40
    )
    empty = ContextRecord(id="bare", kind="lesson", title="bare")
    for item in (full, empty):
        assert record_from_mapping(record_to_mapping(item)) == item
    # Every field is present even when empty, so a record file SAYS it names no
    # successor rather than leaving a reader to infer it.
    assert set(record_to_mapping(empty)) == {
        "id",
        "kind",
        "title",
        "invariant",
        "source_paths",
        "related_ids",
        "last_verified_commit",
        "superseded_by",
    }


def test_a_written_record_loads_back_as_itself(tmp_path):
    store = store_at(tmp_path)
    written = store.write(record(), "feat.json")

    assert written == store.directory / "feat.json"
    loaded, problems = load_records(store.directory)
    assert problems == ()
    assert [item.record for item in loaded] == [record()]


def test_a_record_the_loader_would_refuse_is_never_written(tmp_path):
    """The fail-closed half of `write`: a record that would come back as a
    `RecordProblem` must not replace one that loads today, because that update
    deletes the claim while reporting success."""
    store = store_at(tmp_path)
    store.write(record(), "feat.json")
    before = (store.directory / "feat.json").read_text(encoding="utf-8")

    assert store.write(ContextRecord(id="feat", kind="not-a-kind"), "feat.json") is None
    assert (store.directory / "feat.json").read_text(encoding="utf-8") == before


def test_a_record_file_that_is_not_utf8_is_a_named_problem_not_an_exception(tmp_path):
    """`UnicodeDecodeError` is a `ValueError`, not an `OSError`: a loader that
    guarded only the read would let one such file take every other record with
    it — and a load that raises out of a dispatch is the one outcome worse than
    an index that says which file it could not read."""
    store = store_at(tmp_path)
    store.write(record(), "feat.json")
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "bad.json").write_bytes(b"\xff\xfe not utf-8")

    loaded, problems = load_records(store.directory)

    assert [item.record.id for item in loaded] == ["feat"]
    assert [(p.source, p.message.split(":")[0]) for p in problems] == [("bad.json", "not UTF-8")]


def test_a_write_addresses_nothing_but_a_file_in_its_own_directory(tmp_path):
    store = store_at(tmp_path)
    for name in ("../escape.json", "nested/feat.json", ".hidden.json", "feat", ""):
        assert store.path_for(name) is None
        assert store.write(record(), name) is None
        assert store.repo_path_for(name) == ""
    assert not store.directory.exists()  # nothing was created on the way


def test_a_store_with_no_usable_repository_prefix_can_name_no_path(tmp_path):
    """`repo_path_for` is what the scope check is asked about, so an unusable
    prefix must answer `""` — which every caller reads as out of scope — rather
    than a path `unauthorized_paths` could never match."""
    assert store_at(tmp_path, prefix="docs/context/").repo_path_for("feat.json") == (
        "docs/context/feat.json"
    )
    for bad in ("", "   ", "/abs/docs", "docs/../context", "docs\\context"):
        assert store_at(tmp_path, prefix=bad).repo_path_for("feat.json") == ""


def test_supersede_leaves_the_old_record_present_with_a_resolvable_successor(tmp_path):
    """SUPERSEDE, DO NOT REWRITE — asserted where it is observable: on disk,
    through the index, and through a real resolution."""
    store = store_at(tmp_path)
    old = record("dec-01", kind="decision", invariant="the loop pushes by sha")
    successor = record("dec-02", kind="decision", invariant="the loop pushes by ref")
    store.write(superseded_record(old, successor.id), "dec-01.json")
    store.write(successor, "dec-02.json")

    index = load_index(store.directory)
    assert index.get("dec-01").superseded_by == "dec-02"
    assert index.get("dec-01").invariant == old.invariant  # the reason survives
    assert index.get("dec-02") is not None

    resolution = resolve_context(
        index, ("dec-01",), _NoGit(), max_records=MAX_RECORDS, rev="HEAD"
    )
    assert [f.subject for f in resolution.findings_of(SUPERSEDED)] == ["dec-01"]
    assert resolution.findings_of(DANGLING_SUPERSESSION) == ()
    assert resolution.selected == ()


def test_supersede_refuses_a_successor_nobody_could_follow():
    old = record("dec-01", kind="decision")
    for bad in ("", "  ", " dec-02", "dec-01"):
        try:
            superseded_record(old, bad)
        except ContextRecordError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a successor")
    assert old.superseded_by == ""  # and the original is untouched throughout


class _NoGit:
    """A gateway for a resolution that never reaches a real tree: the one seed
    is superseded, so it is never selected and no record's paths are ever
    compared against a commit."""

    def tree_of(self, rev):
        return f"tree-of-{rev}"

    def tree_entries(self, tree):
        return {}

    def changed_paths(self, a, b):
        return set()


# =============================================================================
# 2. THE FOUR QUESTIONS
# =============================================================================


def test_a_touched_feature_record_in_scope_advances_to_the_published_commit(tmp_path):
    plan = plan_for(tmp_path, record())

    assert [update.record.id for update in plan.updates] == ["feat"]
    assert plan.updates[0].record.last_verified_commit == PUBLISHED
    assert plan.updates[0].repo_path == "docs/context/feat.json"
    assert plan.updates[0].filename == "feat.json"
    assert plan.follow_up == ()


def test_the_record_the_plan_started_from_is_not_mutated(tmp_path):
    original = record(last_verified_commit="a" * 40)
    plan = plan_for(tmp_path, original)

    assert original.last_verified_commit == "a" * 40
    assert plan.updates[0].record is not original


def test_a_touched_incident_record_in_scope_advances_too(tmp_path):
    plan = plan_for(tmp_path, record("inc-01", kind="incident"))

    assert [update.record.id for update in plan.updates] == ["inc-01"]
    assert plan.updates[0].record.last_verified_commit == PUBLISHED


def test_a_record_whose_paths_the_change_did_not_touch_is_left_alone(tmp_path):
    plan = plan_for(tmp_path, record(source_paths=("elsewhere.py",)))

    assert plan.updates == ()
    assert plan.follow_up == ()  # nothing concrete to change, so nothing is filed


def test_a_record_naming_no_source_paths_is_never_verified(tmp_path):
    """It asserts nothing about files, so nothing about files can have altered
    it — and advancing its verification commit would be a claim nobody made."""
    plan = plan_for(tmp_path, record(source_paths=()))

    assert plan.updates == ()
    assert plan.follow_up == ()


def test_a_touched_record_outside_scope_is_never_written_and_becomes_the_follow_up(
    tmp_path,
):
    plan = plan_for(tmp_path, record(), task=unit(paths=("feature.py",)))

    assert plan.updates == ()
    assert [item.record_id for item in plan.follow_up] == ["feat"]
    assert plan.follow_up[0].repo_path == "docs/context/feat.json"
    assert "outside the completed task's approved paths" in plan.follow_up[0].reason


def test_a_task_with_no_approved_paths_writes_no_record(tmp_path):
    """`effective_approved_paths` returns `()` for an unscoped task, under which
    every path is unauthorized — the same fail-closed answer it already gets for
    every other kind of write, and NOT the vacuous "nothing to compare"."""
    plan = plan_for(tmp_path, record(), task=unit(paths=()))

    assert plan.updates == ()
    assert [item.record_id for item in plan.follow_up] == ["feat"]


def test_a_touched_decision_is_never_rewritten_even_in_scope(tmp_path):
    """Question three is the one the loop may not answer: a successor is a claim
    somebody has to author, and rewriting the old record deletes the reason."""
    plan = plan_for(tmp_path, record("dec-01", kind="decision"))

    assert plan.updates == ()
    assert [item.record_id for item in plan.follow_up] == ["dec-01"]
    assert "never authors a successor" in plan.follow_up[0].reason


def test_a_touched_lesson_is_never_rewritten_even_in_scope(tmp_path):
    plan = plan_for(tmp_path, record("les-01", kind="lesson"))

    assert plan.updates == ()
    assert [item.record_id for item in plan.follow_up] == ["les-01"]


def test_no_published_commit_verifies_nothing_and_erases_no_verification(tmp_path):
    """THE fail-open case. A record advanced to an empty commit is not "left
    unknown": `context_resolver` reads it as never verified, so the write would
    DELETE the commit the record already carried."""
    plan = plan_for(tmp_path, record(), published="")

    assert plan.updates == ()
    assert plan.follow_up == ()
    assert plan.notes and "no published commit" in plan.notes[0]


def test_a_record_already_carrying_the_published_commit_is_not_rewritten(tmp_path):
    plan = plan_for(tmp_path, record(last_verified_commit=PUBLISHED))

    assert plan.updates == ()
    assert plan.follow_up == ()


def test_a_record_this_store_cannot_name_a_file_for_is_out_of_scope(tmp_path):
    """The source file is what a record is written to, so a record the loader
    reported under no name is one nothing may write over."""
    plan = classify_closeout(
        unit(),
        chosen(record()),
        store_at(tmp_path),
        changed_paths=frozenset({"feature.py"}),
        published_sha=PUBLISHED,
        sources={},
        known_record_ids=frozenset({"feat"}),
        known_filenames=frozenset(),
    )

    assert plan.updates == ()
    assert [item.repo_path for item in plan.follow_up] == [""]


# =============================================================================
# 3. QUESTION FOUR — a lesson, on ctx-02's bar and no lower
# =============================================================================


def ledger(*outcomes, budget=ATTEMPT_TASK):
    return tuple(
        format_attempt(i, budget, outcome) for i, outcome in enumerate(outcomes, start=1)
    )


def test_one_mistake_is_not_a_lesson(tmp_path):
    plan = plan_for(tmp_path, ledger=ledger("post_commit_verification_failed"))

    assert plan.updates == ()
    assert any("no lesson qualified" in note for note in plan.notes)


def test_a_round_that_qualifies_for_no_lesson_creates_none(tmp_path):
    """Three reviews and an open round: an outcome that is not a failure never
    counts, and a round with no outcome yet has no mistake to repeat."""
    entries = ledger(*([REASON_SENT_FOR_REVIEW] * 3)) + ledger(
        "dispatched", budget=ATTEMPT_PENDING
    )
    assert repeated_failure(entries) == ("", 0)
    assert plan_for(tmp_path, ledger=entries).updates == ()


def test_the_same_mistake_more_than_once_is_a_lesson(tmp_path):
    entries = ledger(
        "post_commit_verification_failed",
        REASON_SENT_FOR_REVIEW,
        "post_commit_verification_failed",
    )
    assert repeated_failure(entries) == (
        "post_commit_verification_failed",
        LESSON_MIN_OCCURRENCES,
    )

    plan = plan_for(tmp_path, ledger=entries)
    lesson = plan.updates[0].record
    assert lesson.kind == "lesson"
    assert lesson.id == "lesson-t1-post_commit_verification_failed"
    assert lesson.last_verified_commit == PUBLISHED
    # It asserts nothing checkable and about no file, so it can contradict no
    # record a person wrote and can never be reported as verified-by-nobody.
    assert lesson.invariant == ""
    assert lesson.source_paths == ()
    assert "2 times" in lesson.title


def test_a_redo_is_judged_on_what_the_round_achieved():
    """`origin>outcome` is read through `attempt_outcome`: two fault-opened
    rounds that both reached the reviewer are two reviews, not two mistakes."""
    entries = ledger(
        f"browser_session_lost>{REASON_SENT_FOR_REVIEW}",
        f"browser_session_lost>{REASON_SENT_FOR_REVIEW}",
        budget=ATTEMPT_FAULT,
    )
    assert repeated_failure(entries) == ("", 0)


def test_a_lesson_that_already_exists_is_not_written_a_second_time(tmp_path):
    entries = ledger("review_packet_build_failed", "review_packet_build_failed")
    existing = "lesson-t1-review_packet_build_failed"
    plan = classify_closeout(
        unit(),
        (),
        store_at(tmp_path),
        changed_paths=frozenset(),
        published_sha=PUBLISHED,
        sources={},
        known_record_ids=frozenset({existing}),
        known_filenames=frozenset(),
        attempt_ledger=entries,
    )

    assert plan.updates == ()
    assert any("already exists" in note for note in plan.notes)


def test_a_lesson_whose_file_is_out_of_scope_is_filed_and_not_written(tmp_path):
    entries = ledger("review_packet_build_failed", "review_packet_build_failed")
    plan = plan_for(tmp_path, task=unit(paths=("feature.py",)), ledger=entries)

    assert plan.updates == ()
    assert [item.record_id for item in plan.follow_up] == [
        "lesson-t1-review_packet_build_failed"
    ]


# =============================================================================
# 4. THE FOLLOW-UP — one per completed task, through the ordinary gates
# =============================================================================


def registry_with(*tasks) -> TaskRegistry:
    return TaskRegistry(list(tasks))


def test_exactly_one_follow_up_names_the_records_and_the_files_it_would_touch(tmp_path):
    task = unit(paths=("feature.py",))
    plan = plan_for(tmp_path, record(), record("dec-01", kind="decision"), task=task)

    request = follow_up_request(task, plan, PUBLISHED)

    assert request["id"] == f"t1{FOLLOW_UP_SUFFIX}"
    assert request["depends_on"] == ["t1"]
    assert request["approved_paths"] == [
        "docs/context/dec-01.json",
        "docs/context/feat.json",
    ]
    assert request["context_ids"] == ["dec-01", "feat"]
    assert "feat" in request["description"] and "dec-01" in request["description"]
    # Shape-checked by the gate `TaskInbox.submit` uses, not by this test's idea
    # of the shape.
    assert check_request_shape(request) == "task"


def test_the_follow_up_reaches_the_registry_through_the_ordinary_gates(tmp_path):
    """A request that shape-checks and is then refused on drain is a follow-up
    nobody ever reads, so the merge itself is what this asserts."""
    task = unit(paths=("feature.py",))
    plan = plan_for(tmp_path, record(), task=task)
    request = follow_up_request(task, plan, PUBLISHED)

    registry = registry_with(task)
    added, applied, refused = apply_requests(registry, [request])

    assert refused == []
    assert added == [f"t1{FOLLOW_UP_SUFFIX} (priority 100)"]
    filed = registry.get(f"t1{FOLLOW_UP_SUFFIX}")
    assert filed.depends_on == ("t1",)
    assert filed.approved_paths == ("docs/context/feat.json",)
    assert filed.context_ids == ("feat",)
    # The narrow scope is the record file and NOTHING the completed task owned.
    assert "feature.py" not in filed.approved_paths


def test_nothing_to_change_files_nothing(tmp_path):
    assert follow_up_request(unit(), CloseoutPlan(), PUBLISHED) is None


def test_no_follow_up_is_filed_when_no_record_file_can_be_named(tmp_path):
    """A creation with no `approved_paths` is accepted by the registry and can
    never be dispatched, so an unnameable follow-up is reported instead of
    parked in the queue forever."""
    task = unit(paths=("feature.py",))
    plan = plan_for(tmp_path, record(), task=task, store=store_at(tmp_path, prefix=""))

    assert plan.follow_up != ()
    assert follow_up_request(task, plan, PUBLISHED) is None


def test_a_record_id_the_task_graph_cannot_hold_is_named_in_prose_only(tmp_path):
    """Record ids are a broader shape than task ids. ONE unusable entry in
    `context_ids` gets the whole request refused on drain — which loses the
    follow-up while the round reports having filed it."""
    odd = record("a record with spaces")
    task = unit(paths=("feature.py",), cite=())
    plan = plan_for(tmp_path, odd, record(), task=task)
    request = follow_up_request(task, plan, PUBLISHED)

    assert request["context_ids"] == ["feat"]
    assert "a record with spaces" in request["description"]
    assert check_request_shape(request) == "task"
    assert apply_requests(registry_with(task), [request])[2] == []


def test_a_task_id_with_no_room_for_the_suffix_gets_no_follow_up_id():
    assert follow_up_id_for("t1") == f"t1{FOLLOW_UP_SUFFIX}"
    assert follow_up_id_for("x" * 64) == ""
    assert follow_up_id_for("") == ""


# =============================================================================
# 5. BOUND TO THE ROUND'S OWN PACKET
# =============================================================================


def gateway(root) -> GitGateway:
    return GitGateway(Path(root), PolicyEngine(PolicyConfig()))


def worker_repo(tmp_path, name="worker") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    make_repo_from_template(root, branch="main", files=(("feature.py", "one\n"),))
    return root


def commit(repo: Path, rel: str, body: str, message: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").strip()


def rendered_round(tmp_path):
    """A real base, a real published commit, and the packet the round was
    given — everything §5 needs to ask whether the two agree."""
    repo = worker_repo(tmp_path)
    base = run_git(repo, "rev-parse", "HEAD").strip()
    published = commit(repo, "feature.py", "two\n", "the change")
    store = store_at(tmp_path)
    store.write(record(), "feat.json")
    execution = TaskExecution(
        task_id="t1",
        task_branch="autoloop/t1",
        worktree_path=str(repo),
        task_base_sha=base,
        published_sha=published,
    )
    packet = render_context_packet(
        unit(),
        execution,
        gateway(repo),
        load_index(store.directory),
        max_records=MAX_RECORDS,
    )
    return repo, store, execution, packet


def test_the_selection_the_packet_showed_is_the_one_classified(tmp_path):
    repo, store, execution, packet = rendered_round(tmp_path)

    plan, refusal = plan_round_closeout(
        unit(), execution, gateway(repo), store, packet.text, max_records=MAX_RECORDS
    )

    assert refusal == ""
    assert [update.record.id for update in plan.updates] == ["feat"]
    assert plan.updates[0].record.last_verified_commit == execution.published_sha


def test_a_record_directory_that_changed_under_the_loop_refuses_the_closeout(tmp_path):
    """The re-resolution is only the round's selection while the directory it
    reads is the directory the packet was cut from. A record added since is a
    record no round was ever shown."""
    repo, store, execution, packet = rendered_round(tmp_path)
    # The SEED LIST is untouched — the task still cites `feat` and nothing else —
    # so the only thing that moved is the directory: `feat` now relates a second
    # record in, and the resolver follows that edge.
    store.write(record("feat-2", source_paths=("feature.py",)), "feat-2.json")
    store.write(record(related_ids=("feat-2",)), "feat.json")

    plan, refusal = plan_round_closeout(
        unit(), execution, gateway(repo), store, packet.text, max_records=MAX_RECORDS
    )

    assert plan.updates == ()
    assert "not the one this round's packet showed" in refusal


def test_a_round_whose_packet_cannot_be_read_back_classifies_nothing(tmp_path):
    repo, store, execution, _ = rendered_round(tmp_path)

    plan, refusal = plan_round_closeout(
        unit(), execution, gateway(repo), store, "", max_records=MAX_RECORDS
    )

    assert plan.updates == ()
    assert "could not be read back" in refusal


def test_an_empty_selection_is_confirmed_by_the_same_comparison(tmp_path):
    """The trivial case stays honest: the heading carries the COUNT, so a packet
    that showed no record and a packet that showed three are not both matched by
    an empty block."""
    repo, store, execution, packet = rendered_round(tmp_path)
    empty = resolve_context(
        build_index(()), (), gateway(repo), max_records=MAX_RECORDS, rev=execution.task_base_sha
    )

    assert not selection_was_shown(packet.text, empty, None, execution.task_base_sha)


def test_a_base_that_no_longer_resolves_refuses_rather_than_classifying(tmp_path):
    repo, store, execution, packet = rendered_round(tmp_path)
    execution.task_base_sha = "0" * 40

    plan, refusal = plan_round_closeout(
        unit(), execution, gateway(repo), store, packet.text, max_records=MAX_RECORDS
    )

    assert plan.updates == ()
    assert "could not be read" in refusal


# =============================================================================
# 6. THROUGH THE PUSH PATH — the loop's own completion, end to end
# =============================================================================


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class WritingExecutor:
    """Writes `files` into the worktree for `task.id` and reports them as the
    round's changed paths — `test_postcommit_flow.py`'s double, in the shape
    this file needs and nothing more."""

    def __init__(self, worktrees_root, files):
        self.worktrees_root = Path(worktrees_root)
        self.files = dict(files)
        self.calls = 0

    def execute(self, directive, task):
        self.calls += 1
        wt = self.worktrees_root / task.id
        for rel, content in self.files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary="wrote the files",
            details="details",
            validation="placeholder",
            changed_paths=tuple(self.files),
        )


def build_round(
    tmp_path,
    approved_paths,
    records=(("feat", "feature.py"),),
    wire=True,
    records_dir=None,
    config_records_dir="",
):
    """One orchestrator on a real repository, with a record store and an inbox
    wired — the linked-worktree shape `test_postcommit_flow.build_postcommit`
    uses, plus the two things ctx-07 adds.

    `wire=True` passes an EXPLICIT loop-private store, which sits OUTSIDE the
    checkout by default and says what its files are CALLED in it
    (`repo_prefix`): that store writes, so a record written into the observed
    tree would be an uncommitted file the loop cannot commit, and the next
    dispatch would refuse to start against a dirty tree.

    `wire=False` passes none, so the loop DERIVES one from
    `[context] records_dir` — `""` for the deployment that turned records off,
    and a repository-relative directory for the ordinary ctx-16 arrangement,
    where the records are files of the checkout itself. `record_store` is
    returned either way so a test can read the files the loop did not write.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    make_repo_from_template(repo_root, branch="main", files=(("README.md", "hello\n"),))

    git = gateway(repo_root)
    worktrees = WorktreeManager(git, tmp_path / "worktrees")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / "state",
        context=ContextConfig(records_dir=config_records_dir),
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    record_store = ContextRecordStore(records_dir or (tmp_path / "records"), PREFIX)
    for record_id, path in records:
        record_store.write(record(record_id, source_paths=(path,)), f"{record_id}.json")

    task = unit(paths=approved_paths, cite=tuple(r for r, _ in records))
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    inbox = TaskInbox(tmp_path / "inbox")

    def no_client():
        raise AssertionError("no browser client expected in this test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=WritingExecutor(tmp_path / "worktrees", {"feature.py": "two\n"}),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worktrees=worktrees,
        execution_store=execution_store,
        intent_store=IntentStore(tmp_path / "intents"),
        validation_runner=ok_validation,
        task_inbox=inbox,
        context_records=record_store if wire else None,
    )
    return orch, repo_root, worktrees, execution_store, task, record_store, inbox


def push_the_round(orch, repo_root, tmp_path, task):
    """Implement, review, approve — and return the execution record the push
    left behind."""
    orch._dispatch_executor(
        Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task.id)
    )
    orch._step_ready()
    req = orch.state.pending_request
    resp = LastResponse(
        request_id=req.request_id,
        raw="{}",
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    bare = tmp_path / "bare.git"
    run_git(tmp_path, "init", "-q", "--bare", str(bare))
    run_git(repo_root, "remote", "add", "origin", str(bare))
    orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)
    return resp


def test_a_completed_round_in_scope_advances_the_record_to_the_published_commit(tmp_path):
    """THE CLAIM on the real path: the loop pushes, marks the task completed,
    and the record its packet selected now names the commit that published."""
    orch, repo_root, worktrees, execution_store, task, record_store, inbox = build_round(
        tmp_path, approved_paths=("feature.py", "docs/context/")
    )

    push_the_round(orch, repo_root, tmp_path, task)

    execution = execution_store.load(task.id)
    assert execution.published_sha != ""
    # Read back the way the loop reads it, through a gateway that holds the
    # published commit: the record now cites one, and the loader refuses a
    # citation it cannot resolve (ctx-14) — a `load_index` with no gateway
    # would report the record rather than return it.
    stored = build_index(*record_store.load(gateway(repo_root))).get("feat")
    assert stored is not None
    assert stored.last_verified_commit == execution.published_sha
    assert stored.invariant == record().invariant  # nothing else was rewritten
    assert inbox.pending() == []  # nothing was left over to file


def test_a_completed_round_out_of_scope_files_one_follow_up_and_writes_nothing(tmp_path):
    orch, repo_root, worktrees, execution_store, task, record_store, inbox = build_round(
        tmp_path, approved_paths=("feature.py",)
    )
    before = (record_store.directory / "feat.json").read_bytes()

    push_the_round(orch, repo_root, tmp_path, task)

    execution = execution_store.load(task.id)
    assert (record_store.directory / "feat.json").read_bytes() == before

    queued = inbox.pending()
    assert len(queued) == 1
    spec = json.loads(queued[0].read_text(encoding="utf-8"))
    assert spec["id"] == f"{task.id}{FOLLOW_UP_SUFFIX}"
    assert spec["depends_on"] == [task.id]
    assert spec["approved_paths"] == ["docs/context/feat.json"]
    assert spec["context_ids"] == ["feat"]
    assert execution.published_sha in spec["description"]


def test_the_closeout_is_idempotent_when_the_push_path_is_re_entered(tmp_path):
    """Crash recovery re-enters the push path, so the closeout runs twice for
    one completed task. The second one must write nothing new and file nothing
    new — the id is derived from the task, and the queue is asked as well as the
    registry."""
    orch, repo_root, worktrees, execution_store, task, record_store, inbox = build_round(
        tmp_path, approved_paths=("feature.py",)
    )
    push_the_round(orch, repo_root, tmp_path, task)
    after_first = (record_store.directory / "feat.json").read_bytes()
    assert len(inbox.pending()) == 1

    worktree_git = GitGateway(worktrees.path_for(task.id), PolicyEngine(PolicyConfig()))
    orch._close_out_context(task.id, worktree_git)

    assert len(inbox.pending()) == 1
    assert (record_store.directory / "feat.json").read_bytes() == after_first


def test_an_unwired_loop_says_so_in_the_transcript_and_writes_nothing(tmp_path):
    """A deployment that turned records off — no explicit store and
    `[context] records_dir = ""`, which since ctx-16 is the only way to get here.
    "No record directory is wired into this loop" and "the closeout stopped
    working" must not look alike, and SKIPPED must stay reachable: it is one of
    the three outcomes the transcript has to keep apart."""
    orch, repo_root, worktrees, execution_store, task, record_store, inbox = build_round(
        tmp_path,
        approved_paths=("feature.py", "docs/context/"),
        wire=False,
        config_records_dir="",
    )
    entries: list[tuple[str, dict]] = []
    orch._log = lambda event, *args, **kwargs: entries.append(
        (event, kwargs.get("data") or {})
    )
    before = (record_store.directory / "feat.json").read_bytes()

    push_the_round(orch, repo_root, tmp_path, task)

    skipped = [data for event, data in entries if event == "context_closeout_skipped"]
    assert skipped and skipped[0]["reason"] == "no_context_record_store"
    assert (record_store.directory / "feat.json").read_bytes() == before
    assert inbox.pending() == []


def test_a_store_inside_the_observed_checkout_is_refused_before_any_write(tmp_path):
    """The trap this guard exists for: a record written into the observed tree
    is a file the loop cannot commit, and the NEXT write-capable dispatch parks
    the whole loop `primary_checkout_dirty` over it. Refused loudly here rather
    than honoured once and paid for on the next round."""
    orch, repo_root, worktrees, execution_store, task, record_store, inbox = build_round(
        tmp_path,
        approved_paths=("feature.py", "docs/context/"),
        records_dir=tmp_path / "repo" / "docs" / "context",
    )
    # Committed, so the round can start at all: an UNTRACKED record file already
    # makes the observed checkout dirty, which is the same park arriving one
    # round earlier.
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "the records")
    entries: list[tuple[str, dict]] = []
    orch._log = lambda event, *args, **kwargs: entries.append(
        (event, kwargs.get("data") or {})
    )
    before = (record_store.directory / "feat.json").read_bytes()

    push_the_round(orch, repo_root, tmp_path, task)

    refused = [data for event, data in entries if event == "context_closeout_refused"]
    assert refused and "inside the observed checkout" in refused[0]["reason"]
    assert (record_store.directory / "feat.json").read_bytes() == before
    assert inbox.pending() == []


def test_a_closeout_that_cannot_write_still_owes_the_update(tmp_path):
    """A failed write is not a completed one: the record joins the follow-up
    rather than becoming a line nobody acts on."""
    orch, repo_root, worktrees, execution_store, task, record_store, inbox = build_round(
        tmp_path, approved_paths=("feature.py", "docs/context/")
    )
    original = record_store.write
    record_store.write = lambda record_, filename: None

    push_the_round(orch, repo_root, tmp_path, task)
    record_store.write = original

    queued = inbox.pending()
    assert len(queued) == 1
    spec = json.loads(queued[0].read_text(encoding="utf-8"))
    assert spec["context_ids"] == ["feat"]
    assert "the write failed" in spec["description"]


# =============================================================================
# 7. THE SCOPE IS NEVER WIDENED
# =============================================================================


def test_a_closeout_widens_neither_the_task_scope_nor_the_trackers(tmp_path):
    """The acceptance criterion, asserted where it is observable rather than by
    reading the code: after a round that needed an out-of-scope context edit,
    the completed task's own scope and the universal trackers are what they
    were."""
    trackers_before = TRACKER_PATHS
    orch, repo_root, worktrees, execution_store, task, record_store, inbox = build_round(
        tmp_path, approved_paths=("feature.py",)
    )

    push_the_round(orch, repo_root, tmp_path, task)

    assert orch._registry.get(task.id).approved_paths == ("feature.py",)
    assert TRACKER_PATHS == trackers_before
    # AFTER the round, off disk: the persisted registry never gained the record
    # path either. Read rather than byte-compared against the pre-state, because
    # `_mark_task_completed` legitimately rewrites the status in the same file.
    assert "docs/context" not in orch._config.tasks_file.read_text(encoding="utf-8")
    # The record file the follow-up names is authorized on the FOLLOW-UP, and on
    # nothing that already existed.
    assert inbox.pending()


#: What MOVES an existing task's scope in this package: the registry mutator,
#: the inbox kind that reaches it, and the universal grant. None of the three may
#: be reachable from the closeout — "just add the path" is the widening this
#: whole path exists to refuse.
SCOPE_MOVERS = ("set_approved_paths", "KIND_APPROVED_PATHS", "TRACKER_PATHS")


def _referenced_names(tree) -> set[str]:
    """Every identifier the code REFERS TO — names and attributes alike.

    Off the AST rather than the text, for the reason `validation._code_strings`
    reads its own scan that way: this file's prose says `TRACKER_PATHS` several
    times to explain why it must not be touched, and a grep over the source
    cannot tell an explanation from a call.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _function_named(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name!r}")


def test_no_closeout_path_can_reach_a_scope_mutation():
    """A drift guard over the code this path is made of.

    Read through `__module__` rather than by opening a path, for the reason
    `test_tasks.py`'s own source-reading test states: the static selector
    narrows a round to the tests that reach the modules it changed, and a file
    opened by name mentions nothing it can see.
    """
    from autoloop import context_packet as context_packet_module

    closeout = ast.parse(
        Path(sys.modules[context_packet_module.__name__].__file__).read_text(
            encoding="utf-8"
        )
    )
    orchestrator = ast.parse(
        Path(sys.modules[Orchestrator.__module__].__file__).read_text(encoding="utf-8")
    )

    reachable = _referenced_names(closeout)
    for method in ("_close_out_context", "_file_context_follow_up"):
        reachable |= _referenced_names(_function_named(orchestrator, method))

    assert not reachable & set(SCOPE_MOVERS), sorted(reachable & set(SCOPE_MOVERS))
    # And the only scope question any of it asks is the shared matcher's.
    assert "unauthorized_paths" in _referenced_names(closeout)


# =============================================================================
# 8. ctx-16 — THE STORE IS WIRED, AND IT IS THE REPOSITORY'S OWN DIRECTORY
#
# ctx-07 left the closeout able to run and nothing for it to run against:
# `no_context_record_store` on every task, on this loop, twice in an hour. The
# claim here is that a store IS wired, that the closeout runs, and that nothing
# is written where the escape detector would see it.
#
# The design is A: records are files of the TARGET REPOSITORY, versioned and
# reviewed with it, so the loop READS them and a round a reviewer approves
# WRITES them. §8.1 is the store itself, as a pure object. §8.2 drives the whole
# push path, because "the closeout runs", "the checkout is clean afterwards" and
# "the packet carried records that exist" are all claims about a real round.
# =============================================================================


def seed_repository_records(repo_root, *records):
    """Commit `records` into `docs/context/` of `repo_root`, the way a reviewed
    round would have left them — which is the only way a record gets there."""
    directory = Path(repo_root) / PREFIX
    directory.mkdir(parents=True, exist_ok=True)
    for item in records:
        (directory / f"{item.id}.json").write_text(
            json.dumps(record_to_mapping(item), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "context records")
    return directory


def repository_round(tmp_path, approved_paths, *records):
    """`build_round` with NO explicit store and a repository-relative
    `records_dir`, plus those records committed where that names — i.e. exactly
    what `cli._build_orchestrator` produces in production.

    The loop-private store `build_round` builds anyway is NOT what the round
    reads; it is left in place only so the returned tuple keeps its shape, and
    every §8 test ignores it. What the loop reads is `<repo>/docs/context`,
    seeded below and committed, because a record only gets there by being
    reviewed into the repository.
    """
    built = build_round(
        tmp_path,
        approved_paths=approved_paths,
        records=tuple((item.id, "README.md") for item in records),
        wire=False,
        config_records_dir=PREFIX,
    )
    seed_repository_records(built[1], *records)
    return built


def closeout_entries(orch) -> list[tuple[str, dict]]:
    """Capture every transcript entry, the way §6's tests do."""
    entries: list[tuple[str, dict]] = []
    orch._log = lambda event, *args, **kwargs: entries.append(
        (event, kwargs.get("data") or {})
    )
    return entries


def test_the_production_store_is_the_repositorys_own_directory_and_refuses_writes(
    tmp_path,
):
    """§8.1. `[context] records_dir` names a directory OF THE REPOSITORY, and the
    store over it cannot put a byte in that checkout."""
    # THE DEFAULT CONFIG, not a prefix this test chose: "an ordinary completed
    # task" means a loop nobody configured, and a chain pinned only as two
    # halves — "the default is docs/context" over here, "a prefix makes a store"
    # over there — is how a test that describes production passes while
    # production stays inert.
    assert repository_record_store(tmp_path / "repo", ContextConfig().records_dir)

    store = repository_record_store(tmp_path / "repo", PREFIX)

    assert store is not None
    assert store.directory == tmp_path / "repo" / PREFIX
    assert store.repo_path_for("feat.json") == "docs/context/feat.json"
    assert store.writes_directly is False
    assert store.write(record(), "feat.json") is None
    # The refusal is the CLASS's, not a caller's: nothing appeared on disk, and
    # the directory was not even created.
    assert not store.directory.exists()


def test_a_store_is_refused_for_every_location_it_could_not_vouch_for(tmp_path):
    """§8.1, fail-closed. Each of these would otherwise read records out of some
    directory nobody named — and `None` is reported as "no store is wired"."""
    assert repository_record_store(tmp_path, "") is None
    assert repository_record_store(tmp_path, "   ") is None
    assert repository_record_store(tmp_path, "/etc/records") is None
    assert repository_record_store(tmp_path, "../records") is None
    assert repository_record_store(tmp_path, None) is None
    assert repository_record_store(None, PREFIX) is None
    # A RELATIVE root would resolve against whatever directory this process
    # happens to stand in — `Path("")` is `Path(".")`, which is the trap
    # `cli`'s `context explain` guards the same way.
    assert repository_record_store(Path("repo"), PREFIX) is None
    assert repository_record_store("", PREFIX) is None


def test_the_location_guard_still_fires_on_anything_that_would_write_there(tmp_path):
    """§8.1, the fail-open this design is one branch away from.

    The repository store passes the location guard ONLY because it writes
    nothing. So the guard is asked about that exact directory three ways: the
    real store (allowed), an ordinary WRITING store over the same directory
    (refused), and the repository store with the flag flipped to claim it writes
    (refused). A guard that had started keying on the class, or on the directory
    being 'the configured one', would pass the third.
    """
    orch, repo_root, *_ = build_round(
        tmp_path,
        approved_paths=("feature.py", "docs/context/"),
        wire=False,
        config_records_dir=PREFIX,
    )
    store = orch._context_record_store()
    assert store is not None
    assert Path(store.directory).resolve() == (repo_root / PREFIX).resolve()
    assert orch._store_would_write_inside_the_observed_checkout(store) is False

    writing = ContextRecordStore(store.directory, PREFIX)
    assert orch._store_would_write_inside_the_observed_checkout(writing) is True

    store.writes_directly = True
    assert orch._store_would_write_inside_the_observed_checkout(store) is True
    # And the class still refuses the write whatever the flag says, which is why
    # the flag is not the only thing between the loop and a dirty tree.
    assert store.write(record(), "feat.json") is None
    assert not Path(store.directory).exists()


def test_an_object_that_does_not_answer_is_treated_as_one_that_writes(tmp_path):
    """§8.1. `writes_directly` is read through ONE accessor that defaults to
    WRITABLE, so a store that never heard of the flag cannot switch the location
    guard off by omission."""
    orch, repo_root, *_ = build_round(
        tmp_path, approved_paths=("feature.py",), wire=False, config_records_dir=PREFIX
    )

    class Mute:
        directory = repo_root / PREFIX

    assert orch._store_writes_directly(Mute()) is True
    assert orch._store_would_write_inside_the_observed_checkout(Mute()) is True


def test_an_ordinary_completed_task_closes_out_against_a_store_that_exists(tmp_path):
    """§8.2, THE CLAIM. A round that touched a record's own source paths runs the
    closeout, names THIS TASK and THIS RECORD, and reports neither a missing
    store nor a refusal."""
    orch, repo_root, worktrees, execution_store, task, _, inbox = repository_round(
        tmp_path,
        ("feature.py", "docs/context/"),
        record("feat", source_paths=("feature.py",)),
    )
    entries = closeout_entries(orch)

    push_the_round(orch, repo_root, tmp_path, task)

    assert not [d for e, d in entries if e == "context_closeout_skipped"]
    assert not [d for e, d in entries if e == "context_closeout_refused"]
    ran = [d for e, d in entries if e == "context_closeout"]
    assert len(ran) == 1
    assert ran[0]["task_id"] == task.id
    assert ran[0]["published_sha"] == execution_store.load(task.id).published_sha
    # DESIGN A: the record was classified and NOT written — it is a file of the
    # repository, so the update goes through the follow-up and its review.
    assert ran[0]["updated"] == []
    assert ran[0]["writes_directly"] is False
    assert ran[0]["needs_attention"] == ["feat"]
    assert ran[0]["follow_up_task"] == f"{task.id}{FOLLOW_UP_SUFFIX}"
    assert Path(ran[0]["records"]).resolve() == (repo_root / PREFIX).resolve()
    # `updated: []` has three readings and this one is said in words.
    assert any("left to the follow-up" in note for note in ran[0]["notes"])

    queued = inbox.pending()
    assert len(queued) == 1
    spec = json.loads(queued[0].read_text(encoding="utf-8"))
    assert spec["approved_paths"] == ["docs/context/feat.json"]
    # A DEFERRAL IS NOT A FAILED WRITE, and does not borrow its wording.
    assert "the write failed" not in spec["description"]
    assert "a round a reviewer approves does" in spec["description"]


def test_a_closeout_that_finds_nothing_to_change_still_says_it_ran(tmp_path):
    """§8.2, the third outcome. The record exists, the round did not touch its
    source paths, and the transcript must not make that look like a skip or a
    refusal — "the alarm that never fires is the failure this whole roadmap item
    is about"."""
    orch, repo_root, worktrees, execution_store, task, _, inbox = repository_round(
        tmp_path,
        ("feature.py", "docs/context/"),
        record("feat", source_paths=("README.md",)),
    )
    entries = closeout_entries(orch)

    push_the_round(orch, repo_root, tmp_path, task)

    ran = [d for e, d in entries if e == "context_closeout"]
    assert len(ran) == 1
    assert ran[0]["task_id"] == task.id
    assert ran[0]["updated"] == []
    assert ran[0]["needs_attention"] == []
    assert ran[0]["follow_up_skipped"] == "nothing_to_file"
    assert not [d for e, d in entries if e == "context_closeout_skipped"]
    assert inbox.pending() == []


def test_the_observed_checkout_is_byte_clean_after_a_round_that_closed_out(tmp_path):
    """§8.2. The property the whole design turns on: a store INSIDE the tree the
    escape detector watches, and that tree untouched by the round that read it.

    Asserted on the record file's own bytes AND on `git status`, because either
    one alone would miss a failure the other catches — a rewrite in place leaves
    the status dirty, and a new sibling file leaves the original's bytes intact.
    """
    orch, repo_root, worktrees, execution_store, task, _, inbox = repository_round(
        tmp_path,
        ("feature.py", "docs/context/"),
        record("feat", source_paths=("feature.py",)),
    )
    records_dir = repo_root / PREFIX
    before = {path.name: path.read_bytes() for path in sorted(records_dir.iterdir())}

    push_the_round(orch, repo_root, tmp_path, task)

    assert {
        path.name: path.read_bytes() for path in sorted(records_dir.iterdir())
    } == before
    assert run_git(repo_root, "status", "--porcelain") == ""


def test_the_packet_a_round_is_given_carries_records_that_exist(tmp_path):
    """§8.2, ctx-05's half. Before ctx-16 every packet said no index was wired
    and reported every cited id as unresolved; the point of a store is that this
    one names the record."""
    orch, repo_root, worktrees, execution_store, task, _, inbox = repository_round(
        tmp_path,
        ("feature.py", "docs/context/"),
        record("feat", source_paths=("feature.py",)),
    )

    push_the_round(orch, repo_root, tmp_path, task)

    packet = ContextPacketStore(orch._config.context_packets_dir).load(task.id)
    assert packet is not None
    assert "no context record index is wired into this loop yet" not in packet.text
    assert "1 indexed, 0 duplicated id(s), 0 unreadable" in packet.text
    assert "feat" in packet.text
    assert record().invariant in packet.text
    # And the digest the reviewer is shown is the digest of those same bytes.
    assert execution_store.load(task.id).context_packet_sha256 == packet.digest


def test_a_repository_with_no_records_yet_reads_as_empty_and_not_as_unwired(tmp_path):
    """§8.2, the state every repository starts in — including this one, where
    `docs/context/` does not exist. An empty directory and NO MECHANISM must not
    look alike: the first is a repository that has written no records, the second
    is a loop that could not read one if it had."""
    orch, repo_root, worktrees, execution_store, task, _, inbox = build_round(
        tmp_path,
        approved_paths=("feature.py", "docs/context/"),
        wire=False,
        config_records_dir=PREFIX,
    )
    assert not (repo_root / PREFIX).exists()
    entries = closeout_entries(orch)

    push_the_round(orch, repo_root, tmp_path, task)

    packet = ContextPacketStore(orch._config.context_packets_dir).load(task.id)
    assert "no context record index is wired into this loop yet" not in packet.text
    assert "0 indexed, 0 duplicated id(s), 1 unreadable" in packet.text
    # The closeout RAN — it did not report the store missing.
    ran = [d for e, d in entries if e == "context_closeout"]
    assert len(ran) == 1 and ran[0]["task_id"] == task.id
    assert not [
        d
        for e, d in entries
        if e == "context_closeout_skipped" and d["reason"] == "no_context_record_store"
    ]


# -----------------------------------------------------------------------------
# 8.3 THE RECORDS ARE READ AT THE ROUND'S BASE, OUT OF GIT — not off the tree
#
# The packet says `task_base_sha: B1` and grades every record's staleness against
# B1. If the record BYTES came off the observed checkout's working tree they
# would be whatever commit the branch is at now, which is later than B1 on any
# round whose base stayed put while the branch moved: a resumed round on a reused
# worker keeps its stale base by design (`_rebase_execution_if_stale`, wrk-01),
# and an operator committing between the base being recorded and the packet
# being rendered does it by accident (`_observed_base_sha`'s own race). Either
# way the packet would quote B2's bytes under B1's sha. So the repository store
# reads git objects at the base, and these pin that it does — at the loader, at
# the packet, and through the full round with the closeout agreeing.
# -----------------------------------------------------------------------------


def moved_on(repo_root, *records):
    """Commit `records` over the ones already there — the observed branch
    advancing after a task was cut from it."""
    seed_repository_records(repo_root, *records)
    return run_git(repo_root, "rev-parse", "HEAD").strip()


AT_BASE = "feature.py greets exactly once — the claim at the base"
LATER = "feature.py greets twice — the claim the branch moved to"


def test_repository_records_are_read_out_of_git_at_the_revision_asked_for(tmp_path):
    """§8.3, the loader. The store's `directory` is the observed checkout's,
    and that checkout is at B2 on disk; `load(git, B1)` answers B1's bytes and
    `load(git, B2)` answers B2's. Direct children only, blobs only, bare file
    names — the same shape `load_records` gives, so a record loaded from a
    commit can still be named as a repository path and matched against a
    scope."""
    repo_root = worker_repo(tmp_path, "repo")
    moved_on(repo_root, record("feat", invariant=AT_BASE))
    # Two things that are NOT records, committed beside one: a note and a
    # nested file. Neither is a record on disk (`load_records` does not
    # recurse and reads only `*.json`), so neither may be one in a commit.
    (repo_root / PREFIX / "README.md").write_text("about these\n", encoding="utf-8")
    (repo_root / PREFIX / "nested").mkdir()
    (repo_root / PREFIX / "nested" / "deep.json").write_text(
        json.dumps(record_to_mapping(record("deep"))), encoding="utf-8"
    )
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "a note and a nested file")
    b1 = run_git(repo_root, "rev-parse", "HEAD").strip()
    b2 = moved_on(repo_root, record("feat", invariant=LATER))
    assert LATER in (repo_root / PREFIX / "feat.json").read_text(encoding="utf-8")

    store = repository_record_store(repo_root, PREFIX)
    git = gateway(repo_root)

    loaded, problems = store.load(git, b1)
    assert problems == ()
    assert [(item.record.id, item.record.invariant) for item in loaded] == [("feat", AT_BASE)]
    assert loaded[0].source == "feat.json"  # bare, so `repo_path_for` can name it
    assert store.repo_path_for(loaded[0].source) == "docs/context/feat.json"

    loaded, problems = store.load(git, b2)
    assert problems == ()
    assert [(item.record.id, item.record.invariant) for item in loaded] == [("feat", LATER)]


def test_a_repository_store_never_falls_back_to_the_working_tree(tmp_path):
    """§8.3, fail-closed. Handed no gateway or no revision, the store answers
    ONE problem saying which — and not the files on disk, which are exactly
    the bytes this store exists not to read. A base that will not resolve is
    the same: one problem naming it, never an empty directory's reading."""
    repo_root = worker_repo(tmp_path, "repo")
    moved_on(repo_root, record("feat", invariant=LATER))
    store = repository_record_store(repo_root, PREFIX)
    assert (store.directory / "feat.json").exists()

    for git, rev in ((None, ""), (None, "b" * 40), (gateway(repo_root), "")):
        loaded, problems = store.load(git, rev)
        assert loaded == ()
        assert len(problems) == 1
        assert "never reads the working tree" in problems[0].message

    loaded, problems = store.load(gateway(repo_root), "0" * 40)
    assert loaded == ()
    assert len(problems) == 1
    assert "could not be read" in problems[0].message
    assert "0" * 40 in problems[0].message


def test_a_commit_without_the_directory_reads_as_empty_and_says_so(tmp_path):
    """§8.3. The problem a missing directory earns in a commit is the same ONE
    problem it earns on disk, so the packet's `1 unreadable` reading of "this
    repository has written no records" survives the move to git."""
    repo_root = worker_repo(tmp_path, "repo")
    bare = run_git(repo_root, "rev-parse", "HEAD").strip()
    store = repository_record_store(repo_root, PREFIX)

    loaded, problems = store.load(gateway(repo_root), bare)

    assert loaded == ()
    assert [p.source for p in problems] == [PREFIX]
    assert "does not exist at" in problems[0].message and bare in problems[0].message


def test_the_packet_retains_the_base_records_after_the_observed_checkout_moved_on(
    tmp_path,
):
    """§8.3, THE REGRESSION, through the full round. The task is cut from B1;
    between the base being recorded and the packet being rendered the observed
    branch moves to B2, where the record says something else; the packet the
    round is given — and the reviewer's stored copy of it — carries B1's bytes
    under B1's sha, and B2's are nowhere in it.

    And the closeout AGREES: it re-reads the records the same way, at the same
    base, so it runs (and confirms the selection) rather than refusing because
    "the directory has changed". A fix to the packet's reader alone would have
    turned every such round into a `context_closeout_refused`.
    """
    orch, repo_root, worktrees, execution_store, task, _, inbox = repository_round(
        tmp_path,
        ("feature.py", "docs/context/"),
        record("feat", invariant=AT_BASE, source_paths=("feature.py",)),
    )
    b1 = run_git(repo_root, "rev-parse", "HEAD").strip()
    moved: dict[str, str] = {}
    original = orch._context_record_index

    def index_after_the_branch_moved(worktree_git, base_sha):
        # The observed branch advances AFTER this round's base is recorded and
        # BEFORE its records are read — the window `_observed_base_sha`
        # documents, and the state a wrk-01 resumed round is in for its whole
        # dispatch. Committed rather than edited in place: an uncommitted edit
        # would dirty the observed checkout and park the loop one round later.
        moved["b2"] = moved_on(
            repo_root, record("feat", invariant=LATER, source_paths=("feature.py",))
        )
        assert base_sha == b1
        # The worker was cut from B1 and its object database holds B1's blobs —
        # the assumption the whole read rests on, checked rather than assumed.
        assert worktree_git.tree_of(b1)
        return original(worktree_git, base_sha)

    orch._context_record_index = index_after_the_branch_moved
    entries = closeout_entries(orch)

    push_the_round(orch, repo_root, tmp_path, task)

    assert moved["b2"] != b1
    assert LATER in (repo_root / PREFIX / "feat.json").read_text(encoding="utf-8")
    packet = ContextPacketStore(orch._config.context_packets_dir).load(task.id)
    assert packet is not None
    assert f"task_base_sha: {b1}" in packet.text
    assert AT_BASE in packet.text
    assert LATER not in packet.text
    assert execution_store.load(task.id).context_packet_sha256 == packet.digest
    # Both readers read B1: the closeout confirmed the selection and ran.
    assert not [d for e, d in entries if e == "context_closeout_refused"]
    ran = [d for e, d in entries if e == "context_closeout"]
    assert len(ran) == 1 and ran[0]["task_id"] == task.id
    assert ran[0]["needs_attention"] == ["feat"]
    # And nothing was written into the tree that moved.
    assert run_git(repo_root, "status", "--porcelain") == ""


def test_the_orchestrators_index_is_the_base_revisions_not_the_checkouts(tmp_path):
    """§8.3, the accessor itself, with no round around it: asked for B1 while
    the checkout stands at B2, `_context_record_index` answers B1's record —
    and asked for B2, B2's — because it reads through the store rather than
    the store's directory."""
    orch, repo_root, *_ = build_round(
        tmp_path,
        approved_paths=("feature.py",),
        wire=False,
        config_records_dir=PREFIX,
    )
    b1 = moved_on(repo_root, record("feat", invariant=AT_BASE))
    b2 = moved_on(repo_root, record("feat", invariant=LATER))
    git = gateway(repo_root)

    assert orch._context_record_index(git, b1).get("feat").invariant == AT_BASE
    assert orch._context_record_index(git, b2).get("feat").invariant == LATER
    # A loop-private store, by contrast, has no revision to read at and answers
    # its directory whatever sha it is handed: it is unversioned, and it sits
    # outside every checkout because it writes.
    private = ContextRecordStore(tmp_path / "private", PREFIX)
    private.write(record("feat", invariant="private"), "feat.json")
    orch._context_records = private
    assert orch._context_record_index(git, b1).get("feat").invariant == "private"
