"""Audit-finding intake — PROMOTED, ALREADY DONE, or DECLINED, and nothing else.

THE CLAIM these tests exist to break: every finding in the newest audit report
becomes exactly one of three recorded outcomes, the record is durable, and the
dashboard panel shows what is OUTSTANDING rather than everything the report ever
said.

So they are organised around the ways that claim could be TRUE-ish and wrong:

* a promoted task that is not runnable (one approved path, no validation) —
  the exact shape that jams on the attempt ceiling or is refused by the reviewer,
* work filed a second time because nobody checked what already covers it,
* two findings minting ONE task id while the first request is still QUEUED —
  `tasks.json` cannot see an undrained request, so the drain applies the first,
  `add_many` refuses the second, and both ledger entries claim a runnable task,
* ALREADY DONE asserted from the operator's sentence rather than from the tree
  (an echo), or satisfied by a file having been DELETED (fail-open),
* an unreadable ledger reading as "nothing recorded", which silently un-filters
  the panel and would let one write destroy every outcome in it,
* a decline that does not survive to the next run,
* a ledger key and a panel row id that are two different spellings of one id.

Hermetic: no git, no network, no model, no subprocess. Every claim here is about
parsing, a JSON file and a directory, so a real repository would be dead weight
(CLAUDE.md, "Prefer the cheapest test that can fail for the right reason").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoloop import dashboard
from autoloop.inbox import (
    AUDIT_OUTCOMES,
    OUTCOME_ALREADY_DONE,
    OUTCOME_DECLINED,
    OUTCOME_PROMOTED,
    AuditFinding,
    IntakeError,
    TaskInbox,
    audit_intake_file,
    audit_intake_summary,
    decline_finding,
    finding_task_id,
    load_audit_intake,
    newest_audit_report,
    outstanding_findings,
    parse_audit_findings,
    promote_finding,
    promotion_spec,
    read_audit_intake,
    record_audit_outcome,
    record_finding_already_done,
    verify_already_done,
)

# One report in exactly the shape `audit/report._finding_block` writes: the
# severity bullet has no colon, files are backticked and comma-joined, and the
# heading is level four with an em dash.
REPORT = """\
# Repository audit — 2026-09-01

## Confirmed defects (2)
#### db_migrations:db-01 — Author a new baseline migration
- severity **critical**, confidence **confirmed**, domain `db_migrations`
- files: `backend/migrations/006_video.py`, `backend/models.py`
- symbols: `006_video.py:16-20`
- evidence: Grepped CREATE TABLE across 37 migration files: zero matches.
- impact: `alembic upgrade head` fails on an empty database.
- acceptance: alembic upgrade head succeeds against an empty database
- validation: createdb fresh_test && alembic upgrade head

#### security_paths:paths-02 — Confine the write-capable agent subprocess
- severity **high**, confidence **confirmed**, domain `security_paths`
- files: `autoloop/implement_executor.py`
- evidence: build_argv carries only -p/--model/--output-format.
- impact: A misbehaving agent can write anywhere the orchestrator can.

## Optional improvements (1)
#### docs_drift:doc-01 — Rename the tracker heading
- severity **low**, confidence **probable**, domain `docs_drift`
- files: `docs/SUMMARY.md`
- evidence: docs/SUMMARY.md:1 says "Summary".
- impact: A reader looks in the wrong file.
"""

#: Two findings whose QUALIFIED IDS differ — so the parser keeps both, and the
#: ledger keys them apart — while the task ids derived from them are the SAME
#: string: `finding_task_id` reduces every run of non-alphanumerics to `-`, so
#: `db-01` and `db_01` land on `audit-db-migrations-db-01` together. The
#: registry cannot see that collision while the first request is still queued,
#: which is what `test_two_findings_whose_task_ids_collide_*` is about.
COLLIDING_REPORT = """\
# Repository audit — 2026-09-01

## Confirmed defects (2)
#### db_migrations:db-01 — Author a new baseline migration
- severity **critical**, confidence **confirmed**, domain `db_migrations`
- files: `backend/migrations/006_video.py`, `backend/models.py`

#### db_migrations:db_01 — Move the Docker build context to the repo root
- severity **critical**, confidence **confirmed**, domain `db_migrations`
- files: `Dockerfile`, `docker-compose.yml`
"""

APP_VALIDATION = (("python3", "-m", "pytest", "tests/", "-q"),)
LOOP_VALIDATION = (("ruff", "check", "."),)


def make_repo(tmp_path: Path, text: str = REPORT) -> Path:
    """A directory with one audit report in it. Not a git checkout — nothing
    under test here asks git anything."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "AUDIT_2026-09-01.md").write_text(text, encoding="utf-8")
    return repo


def write_tasks(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"tasks": rows}), encoding="utf-8")
    return path


def state_for(tmp_path: Path, *, tasks: list[dict] | None = None, text: str = REPORT):
    repo = make_repo(tmp_path, text)
    return repo, read_audit_intake(
        repo,
        report_glob="docs/AUDIT_*.md",
        tasks_file=write_tasks(tmp_path, tasks or []),
        intake_dir=tmp_path / "intake",
    )


# ---- one parser, and it reads what the writer writes -------------------------


def test_the_parser_reads_the_fields_the_report_writer_emits():
    findings = parse_audit_findings(REPORT, "AUDIT_2026-09-01.md")

    assert [f.qualified_id for f in findings] == [
        "db_migrations:db-01",
        "security_paths:paths-02",
        "docs_drift:doc-01",
    ]
    first = findings[0]
    assert first.severity == "critical", "the severity bullet carries no colon"
    assert first.priority == 1, "a severity word must become a sortable priority"
    assert first.affected_files == (
        "backend/migrations/006_video.py",
        "backend/models.py",
    )
    assert "alembic upgrade head" in first.acceptance
    assert first.source == "AUDIT_2026-09-01.md"
    assert findings[2].severity == "low"


def test_the_two_intake_doors_fingerprint_a_finding_identically(tmp_path):
    """`intake suggest` offers unactioned findings and `intake audit` decides
    about them. They read the SAME report through two entry points, so a decline
    recorded by one has to be recognised by the other — which holds only while
    both compute the same fingerprint for one finding."""
    from autoloop.inbox import audit_finding_suggestions, load_audit_intake

    repo = make_repo(tmp_path)
    intake_dir = tmp_path / "intake"
    finding = parse_audit_findings(REPORT, "AUDIT_2026-09-01.md")[2]
    decline_finding(finding, intake_dir=intake_dir, reason="not now")

    offered = audit_finding_suggestions(
        repo, "docs/AUDIT_*.md", "{}", ledger=load_audit_intake(intake_dir)
    )

    assert "audit_finding:docs_drift:doc-01" not in {s.key for s in offered}
    assert len(offered) == 2, "the other two are still offered"


def test_a_findings_fields_stop_at_the_next_heading():
    """Without that, a finding absorbs the bullets of the section after it and
    is promoted with another finding's files in its scope."""
    findings = parse_audit_findings(REPORT)

    assert findings[2].affected_files == ("docs/SUMMARY.md",)
    assert findings[1].affected_files == ("autoloop/implement_executor.py",)


def test_the_ledger_key_and_the_panel_row_id_are_the_same_string(tmp_path):
    """Two spellings of "what is a finding id" is how a declined finding keeps
    appearing on the panel with nothing saying why. The panel reads its rows
    through the same parser the ledger is keyed on."""
    repo = make_repo(tmp_path)
    rows = dashboard.app_tasks(repo)
    parsed = parse_audit_findings(REPORT)

    assert [r["id"] for r in rows] == [f.qualified_id for f in parsed]


# ---- ALREADY DONE: evidence re-read from the tree ---------------------------


def test_a_finding_satisfied_in_the_tree_is_already_done_and_files_no_task(tmp_path):
    repo, state = state_for(tmp_path)
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "models.py").write_text("CREATE TABLE video\n", encoding="utf-8")
    finding = state.finding("db_migrations:db-01")
    inbox = TaskInbox(tmp_path / "inbox")

    outcome = record_finding_already_done(
        finding,
        repo=repo,
        intake_dir=tmp_path / "intake",
        checks=["backend/models.py:CREATE TABLE video"],
        note="the baseline landed under rt-02",
    )

    assert outcome.outcome == OUTCOME_ALREADY_DONE
    assert outcome.evidence == ("backend/models.py contains 'CREATE TABLE video'",)
    assert "the baseline landed under rt-02" in outcome.detail
    assert inbox.pending() == [], "ALREADY DONE must create no task at all"
    stored = json.loads(audit_intake_file(tmp_path / "intake").read_text())
    assert stored["db_migrations:db-01"]["outcome"] == OUTCOME_ALREADY_DONE
    assert stored["db_migrations:db-01"]["evidence"] == [
        "backend/models.py contains 'CREATE TABLE video'"
    ]


def test_already_done_is_refused_when_the_tree_disagrees(tmp_path):
    """The operator's sentence is a QUESTION about the tree, never the evidence.
    If reading the file does not agree, nothing is recorded."""
    repo, state = state_for(tmp_path)
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "models.py").write_text("nothing here\n", encoding="utf-8")
    finding = state.finding("db_migrations:db-01")

    with pytest.raises(IntakeError) as exc:
        record_finding_already_done(
            finding,
            repo=repo,
            intake_dir=tmp_path / "intake",
            checks=["backend/models.py:CREATE TABLE video"],
        )

    assert "does not contain" in str(exc.value)
    assert not audit_intake_file(tmp_path / "intake").exists()


def test_a_missing_file_never_satisfies_a_negated_check(tmp_path):
    """THE fail-open of this feature. `path:!needle` asks "is the bug gone?",
    and a deleted or renamed file makes any needle trivially absent — so the
    guard would switch itself off exactly when the file it grades disappeared."""
    repo = make_repo(tmp_path)

    satisfied, confirmed, problems = verify_already_done(
        repo, ["backend/gone.py:!INSERT INTO user_video_category"]
    )

    assert satisfied is False
    assert confirmed == ()
    assert "no such file" in problems[0]


def test_evidence_with_no_checks_is_refused(tmp_path):
    repo = make_repo(tmp_path)

    satisfied, _, problems = verify_already_done(repo, [])

    assert satisfied is False
    assert "at least one check" in problems[0]


def test_evidence_cannot_point_outside_the_checkout(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "secret.txt").write_text("hello\n", encoding="utf-8")

    satisfied, _, problems = verify_already_done(repo, ["../secret.txt:hello"])

    assert satisfied is False
    assert "outside the repository" in problems[0]


def test_a_negated_check_passes_only_when_the_file_is_there_without_it(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "m.py").write_text("upgrade()\n", encoding="utf-8")

    satisfied, confirmed, problems = verify_already_done(
        repo, ["backend/m.py:!DROP TABLE"]
    )

    assert satisfied is True and problems == ()
    assert confirmed == ("backend/m.py no longer contains 'DROP TABLE'",)


# ---- deduplication ----------------------------------------------------------


def test_a_finding_an_existing_task_covers_is_not_filed_a_second_time(tmp_path):
    repo, state = state_for(
        tmp_path,
        tasks=[
            {
                "id": "rt-02",
                "title": "Author a new baseline migration",
                "description": "Closes db_migrations:db-01 from the 2026-09-01 audit.",
                "status": "completed",
            }
        ],
    )
    finding = state.finding("db_migrations:db-01")
    inbox = TaskInbox(tmp_path / "inbox")

    outcome = promote_finding(
        finding,
        state.assess(finding),
        inbox=inbox,
        intake_dir=tmp_path / "intake",
        app_test_root="tests",
        app_validation=APP_VALIDATION,
        loop_validation=LOOP_VALIDATION,
    )

    assert inbox.pending() == [], "a covered finding must not queue a second task"
    assert outcome.outcome == OUTCOME_PROMOTED
    assert outcome.task_id == "rt-02"
    assert "already covered by rt-02" in outcome.detail
    # And it leaves the outstanding list, so the panel stops asking for it.
    ledger = load_audit_intake(tmp_path / "intake")
    remaining = outstanding_findings(state.report.findings, ledger)
    assert "db_migrations:db-01" not in {f.qualified_id for f in remaining}


def test_promoting_the_same_finding_twice_queues_one_task(tmp_path):
    """`list` stops offering a promoted finding, but an operator can still type
    its id. The second run must not queue a second task for the same work."""
    repo, state = state_for(tmp_path)
    intake_dir = tmp_path / "intake"
    inbox = TaskInbox(tmp_path / "inbox")
    kwargs = dict(
        inbox=inbox,
        intake_dir=intake_dir,
        app_test_root="tests",
        app_validation=APP_VALIDATION,
        loop_validation=LOOP_VALIDATION,
    )
    finding = state.finding("db_migrations:db-01")
    promote_finding(finding, state.assess(finding), **kwargs)

    _, reread = state_for(tmp_path)
    with pytest.raises(IntakeError) as exc:
        promote_finding(finding, reread.assess(finding), **kwargs)

    assert "already recorded as promoted" in str(exc.value)
    assert len(inbox.pending()) == 1


def test_promotion_refuses_an_id_the_registry_already_holds(tmp_path):
    """The merge would refuse it as a duplicate — but only AFTER the ledger had
    recorded that a task exists for the finding."""
    repo, state = state_for(
        tmp_path, tasks=[{"id": "audit-db-migrations-db-01", "title": "something else"}]
    )
    inbox = TaskInbox(tmp_path / "inbox")
    finding = state.finding("db_migrations:db-01")

    with pytest.raises(IntakeError) as exc:
        promote_finding(
            finding,
            state.assess(finding),
            inbox=inbox,
            intake_dir=tmp_path / "intake",
            app_test_root="tests",
            app_validation=APP_VALIDATION,
            loop_validation=LOOP_VALIDATION,
        )

    assert "already holds a task called" in str(exc.value)
    assert inbox.pending() == []
    assert not audit_intake_file(tmp_path / "intake").exists()


def _collision_kwargs(tmp_path, inbox):
    return dict(
        inbox=inbox,
        intake_dir=tmp_path / "intake",
        app_test_root="tests",
        app_validation=APP_VALIDATION,
        loop_validation=LOOP_VALIDATION,
    )


def test_two_findings_whose_task_ids_collide_do_not_share_one_queued_request(tmp_path):
    """`tasks.json` cannot see an UNDRAINED request, so the registry check is
    blind to a second finding minting the same task id. Both requests would
    queue, the drain would apply the first and `add_many` would refuse the
    second — while both ledger entries claimed a runnable task existed."""
    repo, state = state_for(tmp_path, text=COLLIDING_REPORT)
    inbox = TaskInbox(tmp_path / "inbox")
    kwargs = _collision_kwargs(tmp_path, inbox)
    first = state.finding("db_migrations:db-01")
    second = state.finding("db_migrations:db_01")
    assert finding_task_id(first) == finding_task_id(second), (
        "the fixture has stopped colliding, so this test would pass vacuously"
    )

    outcome = promote_finding(first, state.assess(first), **kwargs)
    queued = Path(outcome.queued)
    before = queued.read_bytes()

    # A second, independent read — nothing has drained, so the registry still
    # holds no task at all and the collision is invisible to it.
    _, reread = state_for(tmp_path, text=COLLIDING_REPORT)
    assert reread.assess(second).existing_task_ids == ()
    with pytest.raises(IntakeError) as exc:
        promote_finding(second, reread.assess(second), **kwargs)

    assert "--task-id" in str(exc.value), "a refusal with no remedy is a dead end"
    assert queued.name in str(exc.value), "name the file that already claims the id"
    # The first request is still there, byte for byte, and still the FIRST
    # finding's — not overwritten by, or shared with, the second.
    assert inbox.pending() == [queued]
    assert queued.read_bytes() == before
    assert "db_migrations:db-01" in json.loads(before.decode("utf-8"))["description"]
    # And nothing was recorded for the second finding, so it stays outstanding
    # rather than claiming a task that will never exist.
    _, after = state_for(tmp_path, text=COLLIDING_REPORT)
    assert {f.qualified_id for f in after.outstanding()} == {"db_migrations:db_01"}
    assert list(load_audit_intake(tmp_path / "intake").records) == ["db_migrations:db-01"]


def test_the_remedy_the_collision_refusal_names_actually_works(tmp_path):
    """`--task-id` is what the refusal above tells the operator to reach for, so
    it has to file the second finding rather than refuse it again."""
    repo, state = state_for(tmp_path, text=COLLIDING_REPORT)
    inbox = TaskInbox(tmp_path / "inbox")
    kwargs = _collision_kwargs(tmp_path, inbox)
    first = promote_finding(
        state.finding("db_migrations:db-01"),
        state.assess(state.finding("db_migrations:db-01")),
        **kwargs,
    )

    _, reread = state_for(tmp_path, text=COLLIDING_REPORT)
    second_finding = reread.finding("db_migrations:db_01")
    second = promote_finding(
        second_finding,
        reread.assess(second_finding),
        task_id="audit-db-migrations-docker-01",
        **kwargs,
    )

    assert second.task_id == "audit-db-migrations-docker-01"
    assert sorted(p.name for p in inbox.pending()) == sorted(
        [Path(first.queued).name, Path(second.queued).name]
    )
    _, after = state_for(tmp_path, text=COLLIDING_REPORT)
    assert after.outstanding() == ()
    assert after.summary()["promoted"] == 2


def test_an_operator_supplied_task_id_collides_with_a_queued_one_too(tmp_path):
    """The check runs on the RESOLVED spec id, so `--task-id` typed twice is
    caught by the same guard as two ids that derive to one string."""
    repo, state = state_for(tmp_path, text=COLLIDING_REPORT)
    inbox = TaskInbox(tmp_path / "inbox")
    kwargs = _collision_kwargs(tmp_path, inbox)
    first = state.finding("db_migrations:db-01")
    promote_finding(first, state.assess(first), task_id="audit-shared", **kwargs)

    _, reread = state_for(tmp_path, text=COLLIDING_REPORT)
    second = reread.finding("db_migrations:db_01")
    with pytest.raises(IntakeError) as exc:
        promote_finding(
            second, reread.assess(second), task_id="audit-shared", **kwargs
        )

    assert "audit-shared" in str(exc.value)
    assert len(inbox.pending()) == 1


def test_a_queued_request_that_can_never_become_a_task_claims_no_id(tmp_path):
    """Only CREATION requests claim an id, and the kind is resolved the way
    `check_request_shape` resolves it. A `priority` mutation creates no task; an
    unparseable file is moved to `rejected/` by `drain` and creates none either;
    an unknown kind is refused on merge. Treating any of them as a claim would
    refuse a promotion for no reason — and reading an unhashable `[]` kind out
    of `MUTATION_PAYLOAD` would raise `TypeError` instead."""
    repo, state = state_for(tmp_path, text=COLLIDING_REPORT)
    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_priority("audit-db-migrations-db-01", 1)
    queue = tmp_path / "inbox"
    (queue / "20260101T000000Z-0000000000000000000-1.json").write_text(
        "{not json", encoding="utf-8"
    )
    (queue / "20260101T000000Z-0000000000000000000-2.json").write_text(
        json.dumps({"kind": [], "id": "audit-db-migrations-db-01"}), encoding="utf-8"
    )
    (queue / "20260101T000000Z-0000000000000000000-3.json").write_text(
        json.dumps(["not", "an", "object"]), encoding="utf-8"
    )
    finding = state.finding("db_migrations:db-01")

    outcome = promote_finding(
        finding, state.assess(finding), **_collision_kwargs(tmp_path, inbox)
    )

    assert outcome.task_id == "audit-db-migrations-db-01"
    assert outcome.queued is not None


def test_promotion_is_refused_outright_when_the_registry_could_not_be_read(tmp_path):
    """"No task covers this" is not something an unread registry says. Filing
    blind is how work that shipped weeks ago is recreated."""
    repo = make_repo(tmp_path)
    state = read_audit_intake(
        repo,
        report_glob="docs/AUDIT_*.md",
        tasks_file=tmp_path / "does-not-exist.json",
        intake_dir=tmp_path / "intake",
    )
    finding = state.finding("db_migrations:db-01")
    assert state.registry_read is False

    with pytest.raises(IntakeError) as exc:
        promote_finding(
            finding,
            state.assess(finding),
            inbox=TaskInbox(tmp_path / "inbox"),
            intake_dir=tmp_path / "intake",
            app_test_root="tests",
            app_validation=APP_VALIDATION,
            loop_validation=LOOP_VALIDATION,
        )

    assert "was NOT read" in str(exc.value)
    assert not audit_intake_file(tmp_path / "intake").exists()


# ---- PROMOTED: the task has to be runnable ----------------------------------


def test_a_promoted_finding_produces_a_runnable_task(tmp_path):
    """More than one approved path and at least one validation command —
    measured 2026-08-17, rt-09/rt-13/rt-14 had one path and zero validation, and
    a task that shape hits the attempt ceiling or is refused by the reviewer."""
    repo, state = state_for(tmp_path)
    finding = state.finding("db_migrations:db-01")
    inbox = TaskInbox(tmp_path / "inbox")

    outcome = promote_finding(
        finding,
        state.assess(finding),
        inbox=inbox,
        intake_dir=tmp_path / "intake",
        app_test_root="tests",
        app_validation=APP_VALIDATION,
        loop_validation=LOOP_VALIDATION,
    )

    spec = outcome.spec
    assert len(spec["approved_paths"]) > 1
    assert len(spec["validation"]) >= 1
    assert "tests/" in spec["approved_paths"], "the fix must be able to carry its test"
    assert spec["validation"] == [["python3", "-m", "pytest", "tests/", "-q"]], (
        "an app finding is graded by the APP suite, not autoloop/tests"
    )
    assert spec["priority"] == 1, "critical must become a priority the loop can sort"
    assert spec["id"] == "audit-db-migrations-db-01"
    # It really went through the inbox, and the queued file is the request.
    queued = json.loads(Path(outcome.queued).read_text(encoding="utf-8"))
    assert queued["approved_paths"] == spec["approved_paths"]
    assert len(inbox.pending()) == 1


def test_a_finding_about_the_loop_is_graded_by_the_loops_own_suite(tmp_path):
    repo, state = state_for(tmp_path)
    finding = state.finding("security_paths:paths-02")

    spec = promotion_spec(
        finding,
        state.assess(finding),
        app_test_root="tests",
        app_validation=APP_VALIDATION,
        loop_validation=LOOP_VALIDATION,
    )

    assert spec["approved_paths"] == [
        "autoloop/implement_executor.py",
        "autoloop/tests/",
    ]
    assert spec["validation"] == [["ruff", "check", "."]]
    assert "tests/" not in spec["approved_paths"]


def test_promotion_refuses_rather_than_filing_a_one_path_task(tmp_path):
    """A repository that declares no app suite gets a REFUSAL naming what is
    missing. Filing the narrow task anyway is the ceiling jam this whole
    function exists to avoid."""
    repo, state = state_for(tmp_path)
    finding = state.finding("docs_drift:doc-01")

    with pytest.raises(IntakeError) as exc:
        promotion_spec(
            finding,
            state.assess(finding),
            app_test_root="",
            app_validation=APP_VALIDATION,
            loop_validation=LOOP_VALIDATION,
        )

    assert "only 1 approved path" in str(exc.value)


def test_promotion_refuses_when_there_is_no_validation_command(tmp_path):
    repo, state = state_for(tmp_path)
    finding = state.finding("docs_drift:doc-01")

    with pytest.raises(IntakeError) as exc:
        promotion_spec(
            finding,
            state.assess(finding),
            app_test_root="tests",
            app_validation=(),
            loop_validation=LOOP_VALIDATION,
        )

    assert "NO validation command" in str(exc.value)


def test_promotion_refuses_a_finding_whose_files_line_is_unusable(tmp_path):
    text = REPORT.replace("- files: `docs/SUMMARY.md`", "- files: `docs/**/*.md`")
    repo, state = state_for(tmp_path, text=text)
    finding = state.finding("docs_drift:doc-01")

    with pytest.raises(IntakeError) as exc:
        promotion_spec(
            finding,
            state.assess(finding),
            app_test_root="tests",
            app_validation=APP_VALIDATION,
            loop_validation=LOOP_VALIDATION,
        )

    assert "no usable file path" in str(exc.value)


# ---- DECLINED: durable, and it must survive the next run --------------------


def test_a_declined_finding_stays_declined_and_leaves_the_outstanding_list(tmp_path):
    repo, state = state_for(tmp_path)
    finding = state.finding("docs_drift:doc-01")

    decline_finding(
        finding, intake_dir=tmp_path / "intake", reason="the heading is fine as it is"
    )

    # A SECOND, independent read — a new pass, as the next run would make it.
    _, reread = state_for(tmp_path)
    assert "docs_drift:doc-01" not in {f.qualified_id for f in reread.outstanding()}
    assert reread.summary()["declined"] == 1
    assert reread.summary()["outstanding"] == 2
    record = reread.ledger.record_for(finding)
    assert record["outcome"] == OUTCOME_DECLINED
    assert record["detail"] == "the heading is fine as it is"


def test_declining_needs_a_reason(tmp_path):
    _, state = state_for(tmp_path)

    with pytest.raises(IntakeError):
        decline_finding(
            state.finding("docs_drift:doc-01"), intake_dir=tmp_path / "intake", reason="  "
        )


def test_a_reworded_finding_reopens(tmp_path):
    """A decision is recorded against the EVIDENCE it was made about. Re-word
    the finding and it is a different sentence, so it is asked again."""
    repo, state = state_for(tmp_path)
    decline_finding(
        state.finding("docs_drift:doc-01"),
        intake_dir=tmp_path / "intake",
        reason="not worth it",
    )

    moved = REPORT.replace(
        "#### docs_drift:doc-01 — Rename the tracker heading",
        "#### docs_drift:doc-01 — Delete the tracker entirely",
    )
    _, reread = state_for(tmp_path, text=moved)

    assert "docs_drift:doc-01" in {f.qualified_id for f in reread.outstanding()}


# ---- the ledger itself ------------------------------------------------------


def test_an_unreadable_ledger_filters_nothing_and_says_so(tmp_path):
    """"Nothing has been recorded" and "we could not read the record" are
    different facts. Collapsing them un-filters the panel silently."""
    repo = make_repo(tmp_path)
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    audit_intake_file(intake_dir).write_text("{not json", encoding="utf-8")

    ledger = load_audit_intake(intake_dir)
    assert ledger.read is False and ledger.note

    report = newest_audit_report(repo, "docs/AUDIT_*.md")
    assert len(outstanding_findings(report.findings, ledger)) == len(report.findings)
    summary = audit_intake_summary(report, ledger)
    assert summary["ledger_read"] is False
    assert summary["outstanding"] == summary["total"]


def test_neither_reader_raises_on_a_file_that_is_not_utf8_text(tmp_path):
    """`UnicodeDecodeError` is a `ValueError`, not an `OSError`, so an
    OSError-only guard lets it past — and both of these are called on the
    dashboard's 2s poll, where an exception is a 500 rather than a note."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "AUDIT_2026-09-01.md").write_bytes(b"\xff\xfe not text")
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    audit_intake_file(intake_dir).write_bytes(b"\xff\xfe not text")

    report = newest_audit_report(repo, "docs/AUDIT_*.md")
    assert report.read is False and "could not be read" in report.note
    ledger = load_audit_intake(intake_dir)
    assert ledger.read is False and "could not be read" in ledger.note


def test_a_report_path_that_is_a_directory_is_a_note_not_a_crash(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "AUDIT_2026-09-01.md").mkdir(parents=True)

    report = newest_audit_report(repo, "docs/AUDIT_*.md")

    assert report.read is False and report.findings == ()
    assert "could not be read" in report.note


def test_an_unreadable_ledger_refuses_the_write_rather_than_overwriting_it(tmp_path):
    """That file is every decision already made. Starting a fresh one to record
    a single outcome would destroy them all."""
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    audit_intake_file(intake_dir).write_text("{not json", encoding="utf-8")
    finding = AuditFinding(qualified_id="a:b-01", title="x")

    with pytest.raises(IntakeError) as exc:
        record_audit_outcome(intake_dir, finding, OUTCOME_DECLINED, detail="no")

    assert "refusing to write" in str(exc.value)
    assert audit_intake_file(intake_dir).read_text() == "{not json"


def test_an_outcome_word_nothing_understands_leaves_the_finding_outstanding(tmp_path):
    """A hand-edited or future ledger must not make a finding vanish under a
    word this code cannot act on."""
    repo = make_repo(tmp_path)
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    finding = parse_audit_findings(REPORT)[2]
    audit_intake_file(intake_dir).write_text(
        json.dumps(
            {
                finding.qualified_id: {
                    "outcome": "maybe",
                    "fingerprint": finding.fingerprint,
                }
            }
        ),
        encoding="utf-8",
    )

    ledger = load_audit_intake(intake_dir)
    assert ledger.read is True
    assert ledger.record_for(finding) is None
    report = newest_audit_report(repo, "docs/AUDIT_*.md")
    assert len(outstanding_findings(report.findings, ledger)) == len(report.findings)


def test_the_ledger_is_a_sibling_of_the_inbox_and_survives_a_drain(tmp_path):
    """`TaskInbox.drain` globs `*.json` in ITS directory and moves anything it
    cannot parse into `rejected/`. A ledger written in there would be eaten by
    the next drain, which destroys the record AND reports a spurious problem."""
    from autoloop.inbox import inbox_dir_for, intake_dir_for

    workers_root = tmp_path / "outside" / "workers"
    intake_dir = intake_dir_for(workers_root, tmp_path)
    inbox = TaskInbox(inbox_dir_for(workers_root, tmp_path))
    inbox.directory.mkdir(parents=True, exist_ok=True)
    record_audit_outcome(
        intake_dir, AuditFinding("a:b-01", "x"), OUTCOME_DECLINED, detail="no"
    )

    assert audit_intake_file(intake_dir).parent != inbox.directory
    assert inbox.drain() == ([], [])
    assert audit_intake_file(intake_dir).exists()


def test_every_outcome_needs_an_account_of_itself(tmp_path):
    intake_dir = tmp_path / "intake"
    finding = AuditFinding(qualified_id="a:b-01", title="x")

    for outcome in AUDIT_OUTCOMES:
        with pytest.raises(IntakeError):
            record_audit_outcome(intake_dir, finding, outcome, detail="   ")


# ---- the dashboard reads it -------------------------------------------------


def test_the_panel_shows_outstanding_findings_not_all_of_them(tmp_path):
    repo, state = state_for(tmp_path)
    intake_dir = tmp_path / "intake"
    assert len(dashboard.app_tasks(repo, intake_dir=intake_dir)) == 3

    decline_finding(
        state.finding("docs_drift:doc-01"), intake_dir=intake_dir, reason="no"
    )
    record_audit_outcome(
        intake_dir,
        state.finding("security_paths:paths-02"),
        OUTCOME_PROMOTED,
        detail="queued as audit-security-paths-paths-02",
        task_id="audit-security-paths-paths-02",
    )

    rows = dashboard.app_tasks(repo, intake_dir=intake_dir)
    assert [r["id"] for r in rows] == ["db_migrations:db-01"]

    panel = dashboard.audit_findings_panel(
        repo, report_glob="docs/AUDIT_*.md", intake_dir=intake_dir
    )
    assert panel["summary"] == {
        "report": "AUDIT_2026-09-01.md",
        "total": 3,
        "outstanding": 1,
        "report_read": True,
        "report_note": "",
        "ledger_read": True,
        "ledger_note": "",
        OUTCOME_PROMOTED: 1,
        OUTCOME_ALREADY_DONE: 0,
        OUTCOME_DECLINED: 1,
    }


def test_the_panel_keeps_naming_its_report_once_everything_is_actioned(tmp_path):
    """Empty is the SUCCESS state once rows are filtered, so a note derived from
    the rows would lose the report's name exactly then."""
    repo, state = state_for(tmp_path)
    intake_dir = tmp_path / "intake"
    for finding in state.report.findings:
        decline_finding(finding, intake_dir=intake_dir, reason="not now")

    panel = dashboard.audit_findings_panel(
        repo, report_glob="docs/AUDIT_*.md", intake_dir=intake_dir
    )

    assert panel["rows"] == []
    assert panel["summary"]["report"] == "AUDIT_2026-09-01.md"
    assert panel["summary"]["total"] == 3
    assert panel["summary"]["outstanding"] == 0


def test_a_caller_that_passes_no_intake_dir_filters_nothing(tmp_path):
    """`app_tasks(repo)` is what every existing caller and test does, and it
    must keep meaning "every finding in the report"."""
    repo = make_repo(tmp_path)

    assert len(dashboard.app_tasks(repo)) == 3
    panel = dashboard.audit_findings_panel(repo, report_glob="docs/AUDIT_*.md")
    assert panel["summary"]["ledger_read"] is False


def test_a_report_that_cannot_be_read_is_reported_not_rendered_as_empty(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()

    report = newest_audit_report(repo, "docs/AUDIT_*.md")
    assert report.read is False
    assert report.findings == ()
    assert "no file matches" in report.note

    for bad in ("/etc/*.md", "../*.md", "~/x/*.md"):
        refused = newest_audit_report(repo, bad)
        assert refused.read is False and refused.findings == ()


def test_a_legacy_table_row_still_renders_and_is_never_filtered(tmp_path):
    """`| rt-01 | P1 | … |` carries no domain, so it has no qualified id and can
    never be a ledger key. Those rows are shown unconditionally — stated here so
    it is a decision rather than an oversight."""
    repo = tmp_path / "legacy"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "AUDIT_2026-01-01.md").write_text(
        "| rt-01 | P1 | Guard the destructive downgrade |\n", encoding="utf-8"
    )
    intake_dir = tmp_path / "intake"
    record_audit_outcome(
        intake_dir,
        AuditFinding(qualified_id="rt-01", title="Guard the destructive downgrade"),
        OUTCOME_DECLINED,
        detail="no",
    )

    rows = dashboard.app_tasks(repo, intake_dir=intake_dir)
    assert [r["id"] for r in rows] == ["rt-01"]


# ---- the CLI wiring ---------------------------------------------------------


def test_the_cli_records_each_of_the_three_outcomes(tmp_path, monkeypatch, capsys):
    """One pass through the real command surface, so the argparse wiring and the
    config it reads are exercised rather than assumed."""
    from autoloop import cli
    from autoloop.config import (
        AutoloopConfig,
        BrowserConfig,
        PolicyConfig,
        RepoConfig,
    )

    repo = make_repo(tmp_path)
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "models.py").write_text("CREATE TABLE video\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    write_tasks(state_dir, [])
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=state_dir,
        workers_root=tmp_path / "w" / "workers",
        repo=RepoConfig(app_test_root="tests", app_validation=APP_VALIDATION),
    )
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: config)
    monkeypatch.chdir(repo)

    assert cli.main(["intake", "audit", "list"]) == 0
    assert "3 OUTSTANDING" in capsys.readouterr().out

    assert cli.main(
        ["intake", "audit", "done", "db_migrations:db-01",
         "--evidence", "backend/models.py:CREATE TABLE video"]
    ) == 0
    assert "ALREADY DONE" in capsys.readouterr().out

    assert cli.main(
        ["intake", "audit", "decline", "docs_drift:doc-01", "--reason", "not now"]
    ) == 0
    assert "DECLINED" in capsys.readouterr().out

    assert cli.main(["intake", "audit", "promote", "security_paths:paths-02"]) == 0
    out = capsys.readouterr().out
    assert "PROMOTED" in out and "autoloop/tests/" in out

    assert cli.main(["intake", "audit", "list"]) == 0
    tail = capsys.readouterr().out
    assert "0 OUTSTANDING" in tail
    assert "1 promoted, 1 already done, 1 declined" in tail
