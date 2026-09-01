"""The context-record contract, the stamping path, and the seeded records.

ONE CLAIM, in four parts, and each part is here because its failure is SILENT:

1. **Every file under `docs/context/` loads through one validator, and a
   malformed record is refused BY NAME with the reason rather than skipped.** A
   loader that skipped what it could not parse would report a healthy tree while
   holding a broken record — the absent-evidence-reads-as-a-pass shape this
   repository has already recorded twice (`docs/SECURITY.md`, brw-19c/brw-19d,
   and the seeded lesson that collects them). So the refusals are asserted one
   by one, by `code` and by the record being NAMED in the message, and the
   loader is shown returning NOTHING when one file in a tree is bad. What
   counts as a record is decided by SUFFIX, case-folded, and never by the first
   character of the name: `.hidden.md` and `.hidden.MD` are records like any
   other. A filename-first exemption let a malformed or unindexed record leave
   the contract by being renamed, so that bypass has its own block below —
   including the operator surface, where it made `check` exit 0 over a tree
   holding a record nothing had validated. The same silence has a second route,
   pinned beside it: a DANGLING symlink is False to `is_file` and to `is_dir`,
   so a sweep of regular files drops it without a word, and every symlink is
   therefore refused. Only a NON-Markdown dropping is stepped over, and it is
   reported.
2. **Path fields are validated by the task registry's own
   `_validate_approved_path`.** Asserted as a CALL (a spy the loader must
   reach), not as two implementations agreeing on a sample: agreement today is
   what drift looks like before it happens.
3. **`last_verified_commit` is a measurement.** Every seeded record is either
   stamped to a commit this repository resolves or explicitly `UNSTAMPED`, and
   the test resolves HEAD itself through `GitGateway` rather than trusting a
   value in a file. No agent in this loop can read HEAD
   (`implement_executor.WRITE_ALLOWED_TOOLS` is Read/Grep/Glob/Edit/Write, and
   the prompt carries no sha), so a sha in a seed record would be fabricated.
   "Resolves" is asked of GIT, not of a regular expression: `b` forty times
   passes `tasks._COMMIT_SHA_RE` and names nothing, so `load_context_records` —
   the one mandatory path in — puts every non-sentinel value to the repository
   and is shown here refusing an unknown object BY NAME, refusing a sha that
   resolves to something that is not a commit, and refusing when git could not
   answer at all. That last one is the fail-open case: a validator that accepted
   a stamp because the repository was unreadable would pass exactly when nothing
   could check it.
4. **A stamp is verified against the COMMIT it names, not against the working
   tree.** `Path.exists` is equally True for a file that is untracked, one
   staged and never committed, and one deleted from HEAD and restored on disk —
   so a worktree check would let a run write HEAD into a record as evidence that
   commit holds pointers it does not. Each of those three shapes has its own
   test below, built as a real repository where the worktree and HEAD disagree,
   and each asserts the refusal AND that every record still reads `UNSTAMPED`.
   The inverse is asserted too, because conflating the two questions in the
   other direction is just as wrong: a path the commit holds stamps fine even
   after the worktree loses it.

Fixture-first, with the real-repository claims at the bottom — the fixtures say
WHY each shape is refused, and the bottom section says the rule survives contact
with this checkout's own records.

There is no `pytest.skip` anywhere in this file, deliberately. The stamping and
seed claims need a real repository, and skipping when one is absent would retire
exactly the check that proves nobody typed a sha, while still reporting green.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gitrepo import make_repo_from_template, run_git

from autoloop import context_records, tasks
from autoloop.context_records import (
    CONTEXT_DIR,
    FIELDS,
    INDEX_NAME,
    REQUIRED_SECTIONS,
    UNSTAMPED,
    ContextRecordError,
    commit_tree_paths,
    load_context_records,
    main,
    missing_paths,
    missing_paths_in_tree,
    parse_record,
    stamp_records,
    unverifiable_records,
)
from autoloop.errors import GitError, TaskGraphError
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Well-formed to `tasks._COMMIT_SHA_RE` and held by no repository on earth.
#: Every "the shape check is not the check" claim below turns on this value
#: passing the regular expression and failing git.
UNKNOWN_SHA = "b" * 40

#: A record that must parse. Every fixture below is this with one field moved,
#: so a failure names the one thing that changed.
VALID: dict[str, str] = {
    "id": "ctx-fixture-one",
    "kind": "feature",
    "status": "active",
    "summary": "A fixture record.",
    "source_paths": "autoloop/context_records.py",
    "test_paths": "autoloop/tests/test_context_records.py",
    "task_ids": "ctx-02",
    "last_verified_commit": UNSTAMPED,
    "superseded_by": "",
}


def body_for(kind: str) -> str:
    sections = REQUIRED_SECTIONS.get(kind, ("What this is",))
    return "\n".join("## " + heading + "\n\nA pointer, not a copy.\n" for heading in sections)


def record_text(body: str | None = None, **overrides: str) -> str:
    fields = dict(VALID, **overrides)
    lines = ["---"]
    for key in FIELDS:
        value = fields[key]
        lines.append(key + ": " + value if value else key + ":")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + (body_for(fields["kind"]) if body is None else body)


def id_of(text: str) -> str:
    for line in text.split("\n"):
        if line.startswith("id: "):
            return line[4:]
    return ""


def build_tree(root: Path, records: dict[str, str], *, index: str | None = None) -> Path:
    """Write `{relative path: text}` under `docs/context/`, with an index that
    lists every record unless the caller states its own."""
    base = root / CONTEXT_DIR
    base.mkdir(parents=True, exist_ok=True)
    for rel, text in records.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if index is None:
        rows = "".join("- %s %s\n" % (id_of(text), rel) for rel, text in records.items())
        index = "# Index\n\n" + rows
    (base / INDEX_NAME).write_text(index, encoding="utf-8")
    return base


def sections_of(body: str) -> dict[str, str]:
    """`{heading: the prose under it}` for every `## ` section in a body."""
    sections: dict[str, list[str]] = {}
    heading = ""
    for line in body.split("\n"):
        if line.startswith("## "):
            heading = line[3:].strip()
            sections[heading] = []
        elif heading:
            sections[heading].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def refusal(text: str, path: str = "docs/context/features/fixture.md") -> ContextRecordError:
    with pytest.raises(ContextRecordError) as excinfo:
        parse_record(text, path)
    return excinfo.value


# ---- the metadata block -------------------------------------------------------


def test_a_valid_record_parses_every_field():
    record = parse_record(record_text(), "docs/context/features/fixture.md")
    assert record.id == "ctx-fixture-one"
    assert record.kind == "feature"
    assert record.status == "active"
    assert record.summary == "A fixture record."
    assert record.source_paths == ("autoloop/context_records.py",)
    assert record.test_paths == ("autoloop/tests/test_context_records.py",)
    assert record.task_ids == ("ctx-02",)
    assert record.last_verified_commit == UNSTAMPED
    assert record.stamped is False
    assert record.superseded_by == ""
    assert record.path == "docs/context/features/fixture.md"
    assert "A pointer, not a copy." in record.body


@pytest.mark.parametrize(
    "text, code",
    [
        ("", "empty_record"),
        ("   \n\n", "empty_record"),
        ("# Not a record\n\nprose\n", "no_front_matter"),
        ("---\nid: ctx-x\n\nbody\n", "unterminated_front_matter"),
        (record_text().replace("\n", "\r\n"), "carriage_return"),
    ],
)
def test_a_file_that_is_not_shaped_like_a_record_is_refused_by_path(text, code):
    error = refusal(text)
    assert error.code == code
    assert "docs/context/features/fixture.md" in str(error)


@pytest.mark.parametrize(
    "line, code",
    [
        ("colour: blue", "unknown_field"),
        ("id: ctx-fixture-two", "duplicate_field"),
        ("  status: active", "indented_field"),
        ("", "blank_field_line"),
        ("no colon here", "bad_field_line"),
        ("summary:no space", "bad_field_line"),
    ],
)
def test_a_bad_metadata_line_is_refused_by_name(line, code):
    text = record_text().replace("---\nid:", "---\n" + line + "\nid:", 1)
    error = refusal(text)
    assert error.code == code


def test_a_missing_field_is_refused_and_named():
    text = record_text().replace("task_ids: ctx-02\n", "")
    error = refusal(text)
    assert error.code == "missing_field"
    assert "task_ids" in str(error)


def test_a_padded_value_is_refused_rather_than_stripped():
    error = refusal(record_text().replace("id: ctx-fixture-one", "id: ctx-fixture-one "))
    assert error.code == "padded_field"


def test_the_body_is_everything_after_the_block_and_a_body_line_cannot_be_a_field():
    """A body line spelling a field name is BODY. The parser records the line it
    found the commit on, so nothing later has to search the file for a key the
    prose can also start a line with."""
    body = "## Intent and boundaries\n\nlast_verified_commit: deadbeef\n"
    text = record_text(body=body, kind="project")
    record = parse_record(text, "docs/context/project.md")
    assert record.last_verified_commit == UNSTAMPED
    assert "last_verified_commit: deadbeef" in record.body
    assert text.split("\n")[record.commit_line] == "last_verified_commit: " + UNSTAMPED


# ---- the fields ---------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"id": "ctx fixture"}, "bad_record_id"),
        ({"id": "-leading-dash"}, "bad_record_id"),
        ({"id": ""}, "bad_record_id"),
        ({"kind": "note"}, "bad_kind"),
        ({"kind": "Feature"}, "bad_kind"),
        ({"status": "open"}, "bad_status"),
        ({"summary": ""}, "empty_summary"),
        ({"summary": "x" * 201}, "long_summary"),
        ({"source_paths": ""}, "no_source_paths"),
        ({"test_paths": ""}, "no_test_paths"),
        ({"task_ids": "", "kind": "lesson"}, "no_task_ids"),
        ({"task_ids": "not a task id!"}, "bad_task_id"),
        ({"task_ids": "ctx-02 ctx-02"}, "duplicate_task_ids"),
        ({"source_paths": "autoloop/tasks.py autoloop/tasks.py"}, "duplicate_source_paths"),
    ],
)
def test_a_field_the_contract_refuses_is_named_with_its_reason(overrides, code):
    error = refusal(record_text(**overrides))
    assert error.code == code
    if "id" in overrides:
        # The id is the thing that failed, so the PATH is what names the record.
        assert "docs/context/features/fixture.md" in str(error)
    else:
        assert "ctx-fixture-one" in str(error)


@pytest.mark.parametrize(
    "value",
    [
        "abc123",
        "0" * 39,
        "0" * 41,
        "A" * 40,
        "HEAD",
        "unstamped",
        "",
    ],
)
def test_a_last_verified_commit_that_is_neither_a_full_sha_nor_the_sentinel_is_refused(value):
    error = refusal(record_text(last_verified_commit=value))
    assert error.code == "bad_last_verified_commit"
    assert "ctx-fixture-one" in str(error)


@pytest.mark.parametrize("digits", [40, 64])
def test_both_sha_shapes_the_registry_accepts_pass_the_SHAPE_check(digits):
    """A PARSE claim, and only that. `parse_record` never asks git — resolving
    the value is `load_context_records`' job, below — so this says the two
    lengths `tasks._COMMIT_SHA_RE` admits get that far, not that either one
    names anything. Neither of these records would LOAD: `a` * 40 is in no
    object database, and a 64-hex sha cannot resolve in a sha1 checkout at all.
    """
    record = parse_record(
        record_text(last_verified_commit="a" * digits), "docs/context/features/fixture.md"
    )
    assert record.stamped is True
    assert tasks._COMMIT_SHA_RE.match(record.last_verified_commit)


# ---- one path validator, not two ----------------------------------------------


def test_path_fields_are_validated_by_the_task_registrys_own_function(monkeypatch):
    """Asserted as a CALL. Two validators that agree on today's sample is what
    drift looks like before it happens, so the claim is that this module reaches
    `tasks._validate_approved_path` itself — not that a copy behaves like it."""
    seen: list[str] = []
    real = tasks._validate_approved_path

    def spy(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(tasks, "_validate_approved_path", spy)
    parse_record(
        record_text(source_paths="autoloop/tasks.py autoloop/git_gateway.py"),
        "docs/context/features/fixture.md",
    )
    assert seen == [
        "autoloop/tasks.py",
        "autoloop/git_gateway.py",
        "autoloop/tests/test_context_records.py",
    ]


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "~/notes.md", "../outside.py", "autoloop/../tasks.py", "autoloop/*.py",
     "autoloop\\tasks.py", "autoloop//tasks.py", "-rf", "autoloop/tasks.py:12"],
)
def test_a_path_shape_the_registry_refuses_is_refused_here_with_the_registrys_reason(path):
    with pytest.raises(TaskGraphError) as registry_says:
        tasks._validate_approved_path(path)
    error = refusal(record_text(source_paths=path))
    assert error.code == "bad_source_path"
    assert "ctx-fixture-one" in str(error)
    assert registry_says.value.args[0].split(":", 1)[1].strip()[:40] in str(error)


def test_a_bad_test_path_is_refused_under_its_own_code():
    assert refusal(record_text(test_paths="../elsewhere.py")).code == "bad_test_path"


def test_a_path_containing_a_space_becomes_two_entries_and_is_caught_by_existence(tmp_path):
    """The one thing the whitespace-separated list cannot express, stated rather
    than argued away: no single entry may contain a space, because the registry's
    own segment rule refuses whitespace, so a path with one parses as two. That
    is not silent — both halves are pointers, and `missing_paths` reports each
    one that does not exist."""
    record = parse_record(
        record_text(source_paths="auto loop.py"), "docs/context/features/fixture.md"
    )
    assert record.source_paths == ("auto", "loop.py")
    assert missing_paths(record, tmp_path) == (
        "auto",
        "loop.py",
        "autoloop/tests/test_context_records.py",
    )
    with pytest.raises(TaskGraphError):
        tasks._validate_approved_path("auto loop.py")


# ---- successors ---------------------------------------------------------------


def test_superseded_without_a_successor_is_refused():
    error = refusal(record_text(status="superseded"))
    assert error.code == "missing_successor"
    assert "ctx-fixture-one" in str(error)


def test_a_successor_named_by_a_record_that_is_not_superseded_is_refused():
    assert refusal(record_text(superseded_by="ctx-fixture-two")).code == "unexpected_successor"


def test_a_record_cannot_supersede_itself():
    error = refusal(record_text(status="superseded", superseded_by="ctx-fixture-one"))
    assert error.code == "self_supersession"


def test_two_successors_on_one_line_are_refused():
    error = refusal(record_text(status="superseded", superseded_by="ctx-a ctx-b"))
    assert error.code == "bad_successor"


def test_a_successor_that_does_not_resolve_is_refused_by_the_loader(tmp_path):
    build_tree(
        tmp_path,
        {
            "features/one.md": record_text(
                status="superseded", superseded_by="ctx-fixture-missing"
            )
        },
    )
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "unknown_successor"
    assert "ctx-fixture-missing" in str(excinfo.value)
    assert "ctx-fixture-one" in str(excinfo.value)


def test_a_resolving_successor_is_accepted(tmp_path):
    build_tree(
        tmp_path,
        {
            "features/one.md": record_text(status="superseded", superseded_by="ctx-fixture-two"),
            "features/two.md": record_text(id="ctx-fixture-two"),
        },
    )
    repository = load_context_records(tmp_path)
    assert {record.id for record in repository.records} == {"ctx-fixture-one", "ctx-fixture-two"}
    assert repository.by_id()["ctx-fixture-one"].superseded_by == "ctx-fixture-two"


def test_a_cycle_of_successors_is_refused_rather_than_walked_forever(tmp_path):
    build_tree(
        tmp_path,
        {
            "features/one.md": record_text(status="superseded", superseded_by="ctx-fixture-two"),
            "features/two.md": record_text(
                id="ctx-fixture-two", status="superseded", superseded_by="ctx-fixture-one"
            ),
        },
    )
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "successor_cycle"


# ---- required sections, and the placeholder that would otherwise pass ---------


@pytest.mark.parametrize("kind", sorted(REQUIRED_SECTIONS))
def test_a_kind_with_required_sections_refuses_a_record_missing_one(kind):
    dropped = REQUIRED_SECTIONS[kind][-1]
    body = body_for(kind).replace("## " + dropped, "## Something else")
    overrides = {"kind": kind, "status": "resolved" if kind == "incident" else "active"}
    error = refusal(record_text(body=body, **overrides))
    assert error.code == "missing_section"
    assert dropped in str(error)


@pytest.mark.parametrize("kind", ["project", "architecture", "decision", "feature"])
def test_a_record_with_metadata_and_no_body_is_refused(kind):
    error = refusal(record_text(body="\n", kind=kind))
    assert error.code == "empty_body"


# ---- the tree -----------------------------------------------------------------


def test_the_loader_refuses_a_malformed_record_by_name_and_returns_nothing(tmp_path):
    """The whole load fails. A loader that returned the good records and dropped
    the bad one would report a healthy tree that is not one."""
    build_tree(
        tmp_path,
        {
            "features/good.md": record_text(),
            "features/broken.md": record_text(id="ctx-fixture-two", kind="rumour"),
        },
    )
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "bad_kind"
    assert "ctx-fixture-two" in str(excinfo.value)
    assert "features/broken.md" in str(excinfo.value)


def test_a_record_hidden_in_a_structural_file_is_refused_not_skipped(tmp_path):
    """`README.md` and `index.md` are not parsed as records, so they are checked
    for the record fence instead — otherwise renaming a broken record would move
    it into the category nothing validates."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / "features" / "README.md").write_text(record_text(kind="rumour"), encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "record_in_structural_file"
    assert "features/README.md" in str(excinfo.value)


def test_a_structural_file_that_is_prose_is_reported_as_structural(tmp_path):
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / "features" / "README.md").write_text("# Features\n\nprose\n", encoding="utf-8")
    repository = load_context_records(tmp_path)
    assert repository.structural == (
        "docs/context/features/README.md",
        "docs/context/" + INDEX_NAME,
    )


def test_a_file_that_is_neither_a_record_nor_navigation_is_refused(tmp_path):
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / "features" / "notes.txt").write_text("loose\n", encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "foreign_file"
    assert "notes.txt" in str(excinfo.value)


def test_a_non_markdown_dotfile_is_ignored_and_said_so_rather_than_passed_over_silently(tmp_path):
    """The whole of the exemption: a dropping no record contract can describe.
    It is REPORTED, never dropped in silence."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / ".DS_Store").write_bytes(b"\x00\x01")
    repository = load_context_records(tmp_path)
    assert repository.ignored == ("docs/context/.DS_Store",)
    assert len(repository.records) == 1


def test_a_dangling_symlink_is_refused_rather_than_stepped_over_in_silence(tmp_path):
    """The same bypass by a different route, and the one a regular-file sweep
    loses: a broken link answers False to `is_file` AND to `is_dir`, so keeping
    only regular files drops it without a word. `features/ghost.md` is a record
    git can hold and nothing could read."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / "features" / "ghost.md").symlink_to(tmp_path / "no" / "such" / "file.md")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "symlinked_entry"
    assert "features/ghost.md" in str(excinfo.value)


def test_a_symlink_that_resolves_is_refused_too(tmp_path):
    """Refusing only the BROKEN ones would leave a record validated as whatever
    it points at today and something else tomorrow. Every symlink is refused,
    so what a record is does not depend on the day it was read."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / "features" / "alias.md").symlink_to(base / "features" / "good.md")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "symlinked_entry"
    assert "features/alias.md" in str(excinfo.value)


# ---- a leading dot is not an exit from the contract ----------------------------
#
# Classification is by SUFFIX, not by the first character of the name. A
# filename-first exemption would let a malformed or unindexed record leave the
# one-validator contract by being renamed — the guard switching itself off for
# exactly the file trying to evade it — so each of these asserts that a Markdown
# dotfile is held to the same contract as any other record.


def test_a_malformed_hidden_markdown_record_is_refused_by_name_not_stepped_over(tmp_path):
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / ".hidden.md").write_text(
        record_text(id="ctx-fixture-two", kind="rumour"), encoding="utf-8"
    )
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "bad_kind"
    assert "ctx-fixture-two" in str(excinfo.value)
    assert "docs/context/.hidden.md" in str(excinfo.value)


def test_a_hidden_markdown_record_the_index_does_not_list_is_refused(tmp_path):
    """The second half of the bypass, and the quieter one: a record that parses
    but that the index never names is a record nobody finds."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / ".hidden.md").write_text(record_text(id="ctx-fixture-two"), encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "unindexed_record"
    assert "ctx-fixture-two" in str(excinfo.value)
    assert ".hidden.md" in str(excinfo.value)


def test_a_hidden_markdown_record_that_is_valid_and_indexed_is_a_record_not_an_ignored_file(
    tmp_path,
):
    """The positive direction, so "refused" is not achieved by refusing every
    dotted name: a well-formed, indexed `.hidden.md` loads AS A RECORD and is
    absent from `ignored`."""
    build_tree(tmp_path, {".hidden.md": record_text()})
    repository = load_context_records(tmp_path)
    assert [record.path for record in repository.records] == ["docs/context/.hidden.md"]
    assert repository.ignored == ()


def test_a_file_named_only_md_is_parsed_rather_than_read_as_a_dotfile(tmp_path):
    """`Path(".md").suffix` is `''`, so a suffix test through `Path` would sort
    this one into the ignorable droppings. The match is on the whole name."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / ".md").write_text("# not a record\n", encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "no_front_matter"
    assert "docs/context/.md" in str(excinfo.value)


def test_an_uppercase_suffix_does_not_buy_the_exemption_back(tmp_path):
    """The same bypass one keystroke along. On a case-preserving filesystem
    `.hidden.MD` is as easy to write as `.hidden.md`, and an exact-case suffix
    test would hand the second spelling the exemption the first was just
    denied. The match is case-folded, so this is a record and is refused."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / ".hidden.MD").write_text("not a record\n", encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "no_front_matter"
    assert "docs/context/.hidden.MD" in str(excinfo.value)


def test_an_uppercase_suffix_is_a_record_even_without_the_dot(tmp_path):
    """The other side of the same fold, stated so the rule is one rule: a
    Markdown file is Markdown whatever the case of its suffix. `NOTES.MD` is a
    record — not one of the structural names, which are spelled exactly — so it
    is parsed and refused rather than reported as a foreign file."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / "features" / "NOTES.MD").write_text("# prose\n", encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "no_front_matter"
    assert "features/NOTES.MD" in str(excinfo.value)


def test_a_hidden_readme_is_a_record_and_not_navigation(tmp_path):
    """`.README.md` is not one of the structural NAMES, so it is a record and is
    parsed as one — a dot cannot borrow the navigation exemption either."""
    base = build_tree(tmp_path, {"features/good.md": record_text()})
    (base / "features" / ".README.md").write_text("# prose\n", encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "no_front_matter"
    assert "features/.README.md" in str(excinfo.value)


def test_two_records_may_not_share_an_id(tmp_path):
    build_tree(
        tmp_path,
        {"features/one.md": record_text(), "features/two.md": record_text()},
    )
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "duplicate_record_id"


def test_a_record_the_index_does_not_list_is_refused(tmp_path):
    build_tree(tmp_path, {"features/one.md": record_text()}, index="# Index\n\nnothing here\n")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "unindexed_record"
    assert "ctx-fixture-one" in str(excinfo.value)


def test_an_index_naming_the_id_but_not_the_path_is_still_refused(tmp_path):
    build_tree(
        tmp_path,
        {"features/one.md": record_text()},
        index="# Index\n\n- ctx-fixture-one\n",
    )
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "unindexed_record"


# ---- absent input is a refusal, never an empty pass ---------------------------


def test_a_missing_context_directory_is_a_refusal(tmp_path):
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "missing_context_dir"


def test_a_tree_with_no_index_is_a_refusal(tmp_path):
    base = tmp_path / CONTEXT_DIR
    (base / "features").mkdir(parents=True)
    (base / "features" / "one.md").write_text(record_text(), encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "missing_index"


def test_an_empty_context_directory_still_needs_an_index(tmp_path):
    (tmp_path / CONTEXT_DIR).mkdir(parents=True)
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "missing_index"


# ---- pointers that no longer resolve ------------------------------------------


def test_missing_paths_names_every_pointer_that_moved(tmp_path):
    build_tree(tmp_path, {"features/one.md": record_text()})
    (tmp_path / "autoloop").mkdir()
    (tmp_path / "autoloop" / "context_records.py").write_text("", encoding="utf-8")
    repository = load_context_records(tmp_path)
    record = repository.records[0]
    assert missing_paths(record, tmp_path) == ("autoloop/tests/test_context_records.py",)
    assert unverifiable_records(repository) == (
        ("ctx-fixture-one", ("autoloop/tests/test_context_records.py",)),
    )


# ---- the commit is put to git, not to a regular expression --------------------


def gateway_for(root) -> GitGateway:
    return GitGateway(Path(root), PolicyEngine(PolicyConfig()))


def head_of(root) -> str:
    return gateway_for(root).head_sha()


def project_record(commit: str = UNSTAMPED) -> str:
    """The one record every repository fixture below holds: a project record
    pointing at the `README.md` the template repository already contains, so its
    pointers resolve and the only variable is the commit it claims."""
    return record_text(
        kind="project",
        source_paths="README.md",
        test_paths="",
        task_ids="",
        last_verified_commit=commit,
    )


def pointing_at(path: str, record_id: str = "ctx-fixture-one") -> str:
    """An UNSTAMPED project record whose only pointer is `path`.

    A `project`, so `test_paths` and `task_ids` may be empty and the ONE thing
    the record claims is that `path` belongs to whatever commit stamps it.
    """
    return record_text(
        kind="project", source_paths=path, test_paths="", task_ids="", id=record_id
    )


def stamped_repo(root: Path, commit: str) -> str:
    """A real repository whose one record claims `commit`."""
    make_repo_from_template(root)
    build_tree(root, {"project.md": project_record(commit)})
    return "docs/context/project.md"


class _SpyGit:
    """The real gateway with every commit question recorded.

    The same discipline as the `_validate_approved_path` spy above: the claim is
    that the LOADER asks, so it is asserted as a call and not as a value that
    happens to be right today.
    """

    def __init__(self, real: GitGateway):
        self._real = real
        self.asked: list[str] = []

    def head_sha(self) -> str:
        return self._real.head_sha()

    def read_commit(self, oid: str) -> dict:
        self.asked.append(oid)
        return self._real.read_commit(oid)

    def object_exists(self, oid: str) -> bool:
        self.asked.append(oid)
        return self._real.object_exists(oid)


class _MuteGit:
    """A repository that cannot answer: every probe raises, as a missing git, an
    unreadable object database or a policy refusal all do."""

    def head_sha(self) -> str:
        raise GitError("no repository here")

    def read_commit(self, oid: str) -> dict:
        raise GitError("cat-file commit failed: not a repository")

    def object_exists(self, oid: str) -> bool:
        raise GitError("cat-file -e failed (rc=128): not a repository")


class _ExplodingGit:
    """A gateway nothing may touch. Any attribute access is the failure."""

    def __getattr__(self, name):
        raise AssertionError("the loader asked git " + name + " with nothing to resolve")


def test_a_commit_this_repository_resolves_is_accepted(tmp_path):
    make_repo_from_template(tmp_path)
    head = head_of(tmp_path)
    build_tree(tmp_path, {"project.md": project_record(head)})
    record = load_context_records(tmp_path).records[0]
    assert record.last_verified_commit == head
    assert record.stamped is True


def test_a_full_sha_this_repository_does_not_hold_is_refused_by_name(tmp_path):
    """The shape check passes and the load still refuses — which is the whole
    point: `tasks._COMMIT_SHA_RE` says the value COULD be a sha, and only git
    says whether it is one."""
    assert tasks._COMMIT_SHA_RE.match(UNKNOWN_SHA)
    stamped_repo(tmp_path, UNKNOWN_SHA)
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "unknown_commit"
    assert "ctx-fixture-one" in str(excinfo.value)
    assert UNKNOWN_SHA in str(excinfo.value)
    assert "docs/context/project.md" in str(excinfo.value)


def test_a_sha_that_resolves_to_something_that_is_not_a_commit_is_refused(tmp_path):
    """`object_exists` answers True for ANY object, so "it is in the database"
    is not the claim a stamp makes. A blob's oid is a real object and a false
    stamp."""
    make_repo_from_template(tmp_path)
    blob = run_git(tmp_path, "hash-object", "-w", "README.md").strip()
    assert tasks._COMMIT_SHA_RE.match(blob)
    build_tree(tmp_path, {"project.md": project_record(blob)})
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code == "unresolvable_commit"
    assert "ctx-fixture-one" in str(excinfo.value)
    assert blob in str(excinfo.value)


def test_a_commit_git_cannot_be_asked_about_is_refused_rather_than_passed(tmp_path):
    """THE fail-open case, and the deterministic statement of it: every probe
    raises, as a missing repository, an unreadable object database or a policy
    refusal each would. The record is refused by name — never accepted on git's
    silence, which is what would make an unreadable repository read as a clean
    bill of health."""
    stamped_repo(tmp_path, UNKNOWN_SHA)
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path, git=_MuteGit())
    assert excinfo.value.code == "unresolvable_commit"
    assert "ctx-fixture-one" in str(excinfo.value)
    assert UNKNOWN_SHA in str(excinfo.value)


def test_a_stamped_record_in_a_tree_that_is_not_a_repository_is_refused(tmp_path):
    """A smoke check of the same rule through the REAL gateway, with no
    repository built under the tree. Which refusal comes back depends on whether
    git found an enclosing repository to answer from — `tmp_path` is normally
    outside one, but that is the environment's choice, not this suite's — so
    only "not a pass" is asserted here. The claim itself is carried by the
    `_MuteGit` test above, which does not depend on where the tree sits."""
    build_tree(tmp_path, {"project.md": project_record(UNKNOWN_SHA)})
    with pytest.raises(ContextRecordError) as excinfo:
        load_context_records(tmp_path)
    assert excinfo.value.code in ("unresolvable_commit", "unknown_commit")
    assert "ctx-fixture-one" in str(excinfo.value)


def test_the_loader_asks_git_about_the_stamped_commit_and_not_the_sentinel(tmp_path):
    """Asserted as membership, not as a call sequence: WHICH probe answers is
    `_verify_commit`'s business and may be reordered, while "the stamped value
    was put to git and the sentinel was not" is the claim."""
    make_repo_from_template(tmp_path)
    head = head_of(tmp_path)
    build_tree(
        tmp_path,
        {
            "project.md": project_record(head),
            "features/one.md": record_text(
                source_paths="README.md",
                test_paths="README.md",
                last_verified_commit=UNSTAMPED,
                id="ctx-fixture-two",
            ),
        },
    )
    spy = _SpyGit(gateway_for(tmp_path))
    repository = load_context_records(tmp_path, git=spy)
    assert len(repository.records) == 2
    assert head in spy.asked
    assert UNSTAMPED not in spy.asked
    assert set(spy.asked) == {head}


def test_a_tree_with_nothing_stamped_asks_git_nothing(tmp_path):
    """The one case that asks git nothing, stated as behaviour so the exemption
    cannot quietly grow: the set the check guards is the stamped records, and
    here it is empty. Every member of a non-empty one reaches git or the load
    raises."""
    stampable(tmp_path)
    repository = load_context_records(tmp_path, git=_ExplodingGit())
    assert [record.stamped for record in repository.records] == [False]


# ---- stamping -----------------------------------------------------------------


class _FakeGit:
    """A gateway that answers `head_sha` with whatever it was handed and passes
    every other question to a real one — the claim under test is what
    `stamp_records` does with the ANSWER, not how git produces it.

    The probes delegate rather than being absent: a fake missing a method the
    code under test may call fails as an `AttributeError`, which reads as a
    broken test rather than as the refusal it should be.
    """

    def __init__(self, head: str, real: GitGateway | None = None):
        self._head = head
        self._real = real

    def head_sha(self) -> str:
        return self._head

    def _delegate(self):
        assert self._real is not None, "this fake was asked a question it cannot answer"
        return self._real

    def read_commit(self, oid: str) -> dict:
        return self._delegate().read_commit(oid)

    def object_exists(self, oid: str) -> bool:
        return self._delegate().object_exists(oid)

    def tree_entries(self, tree: str) -> dict:
        return self._delegate().tree_entries(tree)


class _TreelessGit:
    """Every commit question answered, and no tree ever listed.

    What an unreadable object, a policy refusal or a repository that vanished
    mid-run all look like from `ls-tree`'s side. Two tests turn on it and they
    want OPPOSITE answers, which is why it is one double and not two: a run with
    something pending must REFUSE (the tree is what the pointers are checked
    against, and a tree nobody could read must not verify them by default),
    while a run with nothing pending must SUCCEED, because it has no pointer to
    check and therefore no business asking.
    """

    def __init__(self, real: GitGateway):
        self._real = real

    def head_sha(self) -> str:
        return self._real.head_sha()

    def read_commit(self, oid: str) -> dict:
        return self._real.read_commit(oid)

    def object_exists(self, oid: str) -> bool:
        return self._real.object_exists(oid)

    def tree_entries(self, tree: str) -> dict:
        raise GitError("git ls-tree -r -z " + tree + " failed (rc=128): unreadable object")


class _TreeSpyGit:
    """The real gateway with every tree listing recorded, and `tree_of` fatal.

    `tree_of` is `rev-parse <sha>^{tree}` — a second subprocess for a value the
    commit object already carried, and one that dies with the same status for a
    missing object as for an unreadable one, which is the distinction
    `object_exists` exists to preserve. So reaching for it here is the failure,
    not an implementation detail.
    """

    def __init__(self, real: GitGateway):
        self._real = real
        self.trees: list[str] = []

    def head_sha(self) -> str:
        return self._real.head_sha()

    def read_commit(self, oid: str) -> dict:
        return self._real.read_commit(oid)

    def object_exists(self, oid: str) -> bool:
        return self._real.object_exists(oid)

    def tree_of(self, rev: str) -> str:
        raise AssertionError("the stamp re-derived a tree it had already read from the commit")

    def tree_entries(self, tree: str) -> dict:
        self.trees.append(tree)
        return self._real.tree_entries(tree)


def stampable(root: Path) -> str:
    """A repository with one UNSTAMPED record whose pointers all exist in it."""
    make_repo_from_template(root)
    build_tree(root, {"project.md": project_record()})
    return "docs/context/project.md"


def test_stamping_writes_head_and_a_second_run_changes_nothing(tmp_path):
    rel = stampable(tmp_path)
    head = GitGateway(tmp_path, PolicyEngine(PolicyConfig())).head_sha()

    first = stamp_records(tmp_path)
    assert first.head_sha == head
    assert first.stamped == ("ctx-fixture-one",)
    assert first.already == ()
    after_first = (tmp_path / rel).read_text(encoding="utf-8")
    assert "last_verified_commit: " + head in after_first

    second = stamp_records(tmp_path)
    assert second.stamped == ()
    assert second.already == ("ctx-fixture-one",)
    assert (tmp_path / rel).read_text(encoding="utf-8") == after_first


def test_stamping_changes_exactly_one_line_and_no_other_byte(tmp_path):
    rel = stampable(tmp_path)
    before = (tmp_path / rel).read_text(encoding="utf-8").split("\n")
    stamp = stamp_records(tmp_path)
    after = (tmp_path / rel).read_text(encoding="utf-8").split("\n")
    differing = [index for index, _ in enumerate(before) if before[index] != after[index]]
    assert len(before) == len(after)
    assert len(differing) == 1
    assert before[differing[0]] == "last_verified_commit: " + UNSTAMPED
    assert after[differing[0]] == "last_verified_commit: " + stamp.head_sha


def test_stamping_refuses_a_record_whose_pointers_do_not_exist_and_writes_nothing(tmp_path):
    make_repo_from_template(tmp_path)
    build_tree(
        tmp_path,
        {
            "project.md": record_text(
                kind="project", source_paths="README.md", test_paths="", task_ids=""
            ),
            "features/gone.md": record_text(id="ctx-fixture-two", source_paths="not/here.py"),
        },
    )
    with pytest.raises(ContextRecordError) as excinfo:
        stamp_records(tmp_path)
    assert excinfo.value.code == "unverifiable_record"
    assert "not/here.py" in str(excinfo.value)
    for rel in ("docs/context/project.md", "docs/context/features/gone.md"):
        assert UNSTAMPED in (tmp_path / rel).read_text(encoding="utf-8")


# ---- the stamp is checked against the COMMIT, not against the working tree ----
#
# Three shapes where `Path.exists()` says yes and the commit says no. Each is a
# real repository whose worktree and HEAD DISAGREE, because that disagreement is
# the entire bug: a worktree check writes HEAD into a record as evidence that
# commit holds a pointer it has never held.


def stamp_refusal(root: Path) -> ContextRecordError:
    """`stamp_records` must refuse, and must have written nothing when it did.

    The sweep is `rglob("*")` and not `rglob("*.md")` deliberately. A constrained
    glob in evaluated code makes its file a declared READER of every document it
    matches (`validation._names_document`), and `"*.md"` matches every tracker's
    basename — measured here: it moved a docs-only round from 20 selected test
    files to 21. A bare `"*"` discriminates nothing and is dropped by
    `_glob_constrains`, which is exactly the case that rule exists for.
    """
    with pytest.raises(ContextRecordError) as excinfo:
        stamp_records(root)
    for record in sorted(p for p in (root / CONTEXT_DIR).rglob("*") if p.is_file()):
        if record.name not in (INDEX_NAME, "README.md"):
            assert UNSTAMPED in record.read_text(encoding="utf-8"), record.name
    return excinfo.value


def test_stamping_refuses_a_pointer_that_is_only_an_untracked_file(tmp_path):
    """On disk, in no commit. The worktree answers yes and the stamp must not."""
    make_repo_from_template(tmp_path)
    (tmp_path / "extra.py").write_text("x = 1\n", encoding="utf-8")
    build_tree(tmp_path, {"project.md": pointing_at("extra.py")})
    assert (tmp_path / "extra.py").exists()
    error = stamp_refusal(tmp_path)
    assert error.code == "unverifiable_record"
    assert "extra.py" in str(error)
    assert "ctx-fixture-one" in str(error)


def test_stamping_refuses_a_pointer_that_exists_only_in_the_index(tmp_path):
    """Staged and never committed — the case that distinguishes HEAD's tree from
    the INDEX's. A run that read `write-tree` instead would accept this."""
    make_repo_from_template(tmp_path)
    (tmp_path / "staged.py").write_text("x = 1\n", encoding="utf-8")
    run_git(tmp_path, "add", "staged.py")
    assert run_git(tmp_path, "ls-files", "--", "staged.py").strip() == "staged.py"
    build_tree(tmp_path, {"project.md": pointing_at("staged.py")})
    error = stamp_refusal(tmp_path)
    assert error.code == "unverifiable_record"
    assert "staged.py" in str(error)


def test_stamping_refuses_a_pointer_deleted_from_head_and_restored_on_disk(tmp_path):
    """The sharpest of the three: the file was never absent from the worktree at
    the moment anything looked, and the commit being stamped does not hold it."""
    make_repo_from_template(tmp_path)
    (tmp_path / "gone.py").write_text("x = 1\n", encoding="utf-8")
    run_git(tmp_path, "add", "gone.py")
    run_git(tmp_path, "commit", "-q", "-m", "add gone.py")
    run_git(tmp_path, "rm", "gone.py")
    run_git(tmp_path, "commit", "-q", "-m", "remove gone.py")
    (tmp_path / "gone.py").write_text("x = 1\n", encoding="utf-8")
    build_tree(tmp_path, {"project.md": pointing_at("gone.py")})
    assert (tmp_path / "gone.py").exists()
    error = stamp_refusal(tmp_path)
    assert error.code == "unverifiable_record"
    assert "gone.py" in str(error)


def test_a_pointer_the_commit_holds_is_stamped_even_after_the_worktree_loses_it(tmp_path):
    """The INVERSE fail-open, and the reason this is not just "be stricter": a
    stamp says these pointers were in THAT commit, and they were. Refusing here
    would mean the two questions had been conflated in the other direction."""
    make_repo_from_template(tmp_path)
    build_tree(tmp_path, {"project.md": pointing_at("README.md")})
    (tmp_path / "README.md").unlink()
    stamp = stamp_records(tmp_path)
    assert stamp.stamped == ("ctx-fixture-one",)
    assert "last_verified_commit: " + stamp.head_sha in (
        tmp_path / "docs/context/project.md"
    ).read_text(encoding="utf-8")


def test_a_directory_pointer_resolves_against_a_tree_that_lists_only_blobs(tmp_path):
    """`ls-tree -r` emits no directory entries, and an approved path may end in
    '/'. Without the ancestors synthesised alongside the blobs, a record naming
    a directory the commit plainly contains would be refused as absent."""
    make_repo_from_template(
        tmp_path, files=(("README.md", "hello\n"), ("pkg/mod.py", "x = 1\n"))
    )
    build_tree(tmp_path, {"project.md": pointing_at("pkg/")})
    assert stamp_records(tmp_path).stamped == ("ctx-fixture-one",)


def test_a_pointer_that_is_only_a_prefix_of_a_real_path_is_still_refused(tmp_path):
    """The boundary of the rule above: 'pkg/mo' is a prefix of 'pkg/mod.py' and
    names nothing. Matching prefixes without the separator would accept it."""
    make_repo_from_template(
        tmp_path, files=(("README.md", "hello\n"), ("pkg/mod.py", "x = 1\n"))
    )
    build_tree(tmp_path, {"project.md": pointing_at("pkg/mo")})
    error = stamp_refusal(tmp_path)
    assert error.code == "unverifiable_record"
    assert "pkg/mo" in str(error)


def test_a_tree_git_cannot_list_refuses_rather_than_verifying_every_pointer(tmp_path):
    """The fail-open shape for the new question. An unreadable tree must not
    become a tree in which everything checks out, and it must not become one in
    which nothing does either — it is a refusal naming the tree."""
    rel = stampable(tmp_path)
    with pytest.raises(ContextRecordError) as excinfo:
        stamp_records(tmp_path, git=_TreelessGit(gateway_for(tmp_path)))
    assert excinfo.value.code == "unreadable_tree"
    assert UNSTAMPED in (tmp_path / rel).read_text(encoding="utf-8")


def test_a_run_with_nothing_pending_never_lists_a_tree(tmp_path):
    """The exemption stated as behaviour so it cannot quietly grow: the set the
    tree check guards is the PENDING records, and here it is empty. A gateway
    that refuses every listing still completes the run."""
    make_repo_from_template(tmp_path)
    old = head_of(tmp_path)
    build_tree(tmp_path, {"project.md": project_record(old)})
    stamp = stamp_records(tmp_path, git=_TreelessGit(gateway_for(tmp_path)))
    assert stamp.stamped == ()
    assert stamp.already == ("ctx-fixture-one",)


def test_the_stamp_lists_the_tree_of_the_commit_it_verified_and_asks_once(tmp_path):
    stampable(tmp_path)
    spy = _TreeSpyGit(gateway_for(tmp_path))
    stamp = stamp_records(tmp_path, git=spy)
    assert stamp.stamped == ("ctx-fixture-one",)
    assert spy.trees == [gateway_for(tmp_path).read_commit(stamp.head_sha)["tree"]]


def test_a_file_that_changes_under_the_run_leaves_every_record_untouched(
    tmp_path, monkeypatch
):
    """Every refusal writes NOTHING, including the late one.

    The sentinel re-read happens for all pending records before the first write,
    so a file that changed underneath the run cannot be discovered halfway
    through and leave some records stamped and some not. `project.md` sorts
    AFTER `features/two.md`, so without that ordering `two.md` would already
    carry the sha by the time `project.md` failed.
    """
    make_repo_from_template(tmp_path)
    build_tree(
        tmp_path,
        {
            "project.md": project_record(),
            "features/two.md": record_text(
                id="ctx-fixture-two", source_paths="README.md", test_paths="README.md"
            ),
        },
    )
    real_read = context_records._read
    reads: list[str] = []

    def racing_read(file, rel):
        text = real_read(file, rel)
        if rel == "docs/context/project.md":
            reads.append(rel)
            if len(reads) > 1:
                return text.replace(
                    "last_verified_commit: " + UNSTAMPED, "last_verified_commit: " + "c" * 40
                )
        return text

    monkeypatch.setattr(context_records, "_read", racing_read)
    with pytest.raises(ContextRecordError) as excinfo:
        stamp_records(tmp_path)
    assert excinfo.value.code == "record_changed_under_stamp"
    for rel in ("docs/context/project.md", "docs/context/features/two.md"):
        assert UNSTAMPED in (tmp_path / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("head", ["", "abc1234", "HEAD", "0" * 39, "Z" * 40])
def test_stamping_refuses_a_head_that_is_not_a_full_sha(tmp_path, head):
    rel = stampable(tmp_path)
    with pytest.raises(ContextRecordError) as excinfo:
        stamp_records(tmp_path, git=_FakeGit(head))
    assert excinfo.value.code == "head_unresolved"
    assert UNSTAMPED in (tmp_path / rel).read_text(encoding="utf-8")


def test_a_malformed_record_stops_stamping_before_anything_is_written(tmp_path):
    rel = stampable(tmp_path)
    base = tmp_path / CONTEXT_DIR
    (base / "features").mkdir(parents=True, exist_ok=True)
    (base / "features" / "broken.md").write_text("not a record\n", encoding="utf-8")
    with pytest.raises(ContextRecordError) as excinfo:
        stamp_records(tmp_path)
    assert excinfo.value.code == "no_front_matter"
    assert UNSTAMPED in (tmp_path / rel).read_text(encoding="utf-8")


def test_an_already_stamped_record_is_left_at_the_commit_it_was_verified_at(tmp_path):
    """HEAD moves, the record does not. A stamp says "these pointers were
    checked at this commit", so carrying it forward because the branch advanced
    would assert a verification nobody performed. The old value is a REAL commit
    of this repository — an arbitrary sha would not survive the load at all."""
    make_repo_from_template(tmp_path)
    old = head_of(tmp_path)
    build_tree(tmp_path, {"project.md": project_record(old)})
    run_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "a later commit")

    stamp = stamp_records(tmp_path)
    assert stamp.head_sha != old
    assert stamp.stamped == ()
    assert stamp.already == ("ctx-fixture-one",)
    assert "last_verified_commit: " + old in (
        tmp_path / "docs/context/project.md"
    ).read_text(encoding="utf-8")


def test_stamping_refuses_a_head_no_object_resolves_and_writes_nothing(tmp_path):
    """A HEAD of the right SHAPE that names nothing is refused before a byte is
    written. Otherwise the run would write a sha into the record and only then
    discover, on the read-back, that nothing resolves it — leaving the file
    changed and the tree unloadable."""
    rel = stampable(tmp_path)
    fake = _FakeGit(UNKNOWN_SHA, gateway_for(tmp_path))
    with pytest.raises(ContextRecordError) as excinfo:
        stamp_records(tmp_path, git=fake)
    assert excinfo.value.code == "unknown_commit"
    assert UNKNOWN_SHA in str(excinfo.value)
    assert UNSTAMPED in (tmp_path / rel).read_text(encoding="utf-8")


def test_stamping_refuses_when_an_already_stamped_record_no_longer_resolves(tmp_path):
    """The load runs first, so a record carrying an unresolvable commit stops
    the run — a stamping pass must never write into a tree it could not
    validate."""
    make_repo_from_template(tmp_path)
    build_tree(
        tmp_path,
        {
            "project.md": project_record(UNKNOWN_SHA),
            "features/two.md": record_text(
                id="ctx-fixture-two", source_paths="README.md", test_paths="README.md"
            ),
        },
    )
    rel = "docs/context/project.md"
    with pytest.raises(ContextRecordError) as excinfo:
        stamp_records(tmp_path)
    assert excinfo.value.code == "unknown_commit"
    assert UNSTAMPED in (tmp_path / "docs/context/features/two.md").read_text(encoding="utf-8")
    assert UNKNOWN_SHA in (tmp_path / rel).read_text(encoding="utf-8")


# ---- the entry point ----------------------------------------------------------


def test_check_passes_on_a_valid_tree_and_names_what_is_unstamped(tmp_path, capsys):
    stampable(tmp_path)
    assert main(["check", str(tmp_path)]) == 0
    assert "1 record(s) valid, 1 unstamped" in capsys.readouterr().out


def test_check_names_the_dotfiles_it_stepped_over_rather_than_passing_over_them(
    tmp_path, capsys
):
    """The one category no record contract reaches — a NON-Markdown dropping —
    so `check` says the file is there. Reported, not refused: a `.DS_Store` is
    not a broken record, and a run that exited 1 on one would make the operator
    surface useless. What must not happen is silence, which would leave a file
    sitting under the context tree that nothing validated and nothing
    mentioned."""
    stampable(tmp_path)
    (tmp_path / CONTEXT_DIR / ".DS_Store").write_bytes(b"\x00\x01")
    assert main(["check", str(tmp_path)]) == 0
    assert "ignored (not a record): docs/context/.DS_Store" in capsys.readouterr().err


def test_check_refuses_a_hidden_markdown_record_rather_than_reporting_it_as_ignored(
    tmp_path, capsys
):
    """The operator surface of the same rule, and the one that would be a green
    run over a tree holding an unvalidated record: `check` must EXIT 1 and name
    the file, not print it as ignored and pass."""
    stampable(tmp_path)
    (tmp_path / CONTEXT_DIR / ".hidden.md").write_text(
        record_text(id="ctx-fixture-two", kind="rumour"), encoding="utf-8"
    )
    assert main(["check", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "bad_kind" in err
    assert "docs/context/.hidden.md" in err
    assert "ignored" not in err


def test_check_fails_on_a_pointer_that_moved(tmp_path, capsys):
    build_tree(tmp_path, {"features/one.md": record_text()})
    assert main(["check", str(tmp_path)]) == 1
    assert "unverifiable: ctx-fixture-one" in capsys.readouterr().err


def test_check_fails_on_a_commit_this_repository_cannot_resolve(tmp_path, capsys):
    """`check` is a validation path like any other, so it inherits the loader's
    refusal rather than needing its own copy of it."""
    stamped_repo(tmp_path, UNKNOWN_SHA)
    assert main(["check", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "unknown_commit" in err
    assert "ctx-fixture-one" in err


def test_a_refusal_is_reported_with_its_reason_and_exits_one(tmp_path, capsys):
    assert main(["check", str(tmp_path)]) == 1
    assert "missing_context_dir" in capsys.readouterr().err


def test_stamp_through_the_entry_point_is_re_runnable(tmp_path, capsys):
    stampable(tmp_path)
    assert main(["stamp", str(tmp_path)]) == 0
    assert "stamped 1" in capsys.readouterr().out
    assert main(["stamp", str(tmp_path)]) == 0
    assert "stamped 0" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["explode"], ["check", "a", "b"], [""]])
def test_an_unknown_verb_is_a_usage_error_not_a_pass(argv, capsys):
    assert main(argv) == 2
    assert "usage:" in capsys.readouterr().err


# ---- this checkout's own records ----------------------------------------------


def repo_gateway() -> GitGateway:
    return GitGateway(REPO_ROOT, PolicyEngine(PolicyConfig()))


def test_this_checkout_is_a_git_repository_so_the_claims_below_can_be_checked():
    """Asserted rather than skipped. Every claim under this heading needs git,
    and a skip would retire the check that proves no sha was typed by hand while
    still reporting green — the failure this file exists to refuse."""
    assert (REPO_ROOT / ".git").exists(), "the seeded records are graded against a real HEAD"


def test_every_seeded_record_loads_through_the_one_validator():
    repository = load_context_records(REPO_ROOT)
    assert repository.records, "docs/context/ holds no records"
    kinds = {record.kind for record in repository.records}
    assert {"project", "architecture", "feature", "incident", "decision", "lesson"} == kinds
    assert repository.structural, "the index and the category READMEs are structural"


def test_every_seeded_records_source_and_test_paths_exist_in_this_checkout():
    repository = load_context_records(REPO_ROOT)
    assert unverifiable_records(repository) == ()


def test_every_seeded_record_is_stamped_to_a_commit_that_resolves_or_explicitly_unstamped():
    """HEAD is resolved HERE, by this test, through the gateway — the file's own
    value is never taken as evidence about itself."""
    git = repo_gateway()
    head = git.head_sha()
    assert tasks._COMMIT_SHA_RE.match(head)
    # Exercised even when every record is unstamped, so the probe the loop below
    # depends on is known to answer True for a commit that really exists.
    assert git.object_exists(head) is True
    for record in load_context_records(REPO_ROOT).records:
        if record.last_verified_commit == UNSTAMPED:
            continue
        assert git.object_exists(record.last_verified_commit) is True, (
            record.id + " names a commit this repository cannot resolve"
        )


def test_every_seeded_records_pointers_are_checked_against_the_commit_it_names():
    """The task's claim, stated over this checkout's own records: a record's
    source and test paths exist AT THE COMMIT it names as last verified.

    Not vacuous while every seed is on the sentinel — the else arm asserts the
    only other thing a record is allowed to say, which is what makes "stamped or
    explicitly unstamped" a dichotomy rather than a gap — and it becomes the
    stronger half the moment the stamping path has run here.
    """
    git = repo_gateway()
    for record in load_context_records(REPO_ROOT).records:
        if not record.stamped:
            assert record.last_verified_commit == UNSTAMPED, record.id
            continue
        tree = git.read_commit(record.last_verified_commit)["tree"]
        held = commit_tree_paths(git, tree, record.id)
        assert missing_paths_in_tree(record, held) == (), (
            record.id + " names pointers its own last_verified_commit does not hold"
        )


def test_the_head_tree_of_this_checkout_holds_files_and_the_directories_above_them():
    """`commit_tree_paths` measured against a real repository, so the membership
    rule the stamp turns on is not left to the fixtures alone: a blob git lists,
    a directory it never lists, and the prefix that must NOT match."""
    git = repo_gateway()
    head = git.head_sha()
    held = commit_tree_paths(git, git.read_commit(head)["tree"], "HEAD")
    assert "autoloop/tasks.py" in held
    assert "autoloop" in held
    assert "autoloop/tests" in held
    assert "autoloop/task" not in held
    assert "no/such/file.py" not in held


def test_the_index_lists_every_seeded_record_by_id_and_by_path():
    repository = load_context_records(REPO_ROOT)
    index = (REPO_ROOT / CONTEXT_DIR / INDEX_NAME).read_text(encoding="utf-8")
    for record in repository.records:
        assert record.id in index
        assert record.path[len(CONTEXT_DIR) + 1:] in index


def test_no_seeded_record_is_a_placeholder():
    """Placeholders make a validator's tests pass vacuously. Nothing here can
    prove a record's prose is TRUE — no automated check can — but a stub body, an
    empty section under a required heading, and an unfinished marker are all
    refusable by shape, so they are refused."""
    for record in load_context_records(REPO_ROOT).records:
        assert len(record.body) > 400, record.id + " has a stub body"
        assert record.source_paths
        for marker in ("TODO", "FIXME", "XXX"):
            assert marker not in record.body, record.id + " carries a " + marker
        for heading, prose in sections_of(record.body).items():
            assert len(prose.split()) >= 12, record.id + " says nothing under " + heading


def test_the_context_tree_was_not_added_to_the_always_writable_tracker_paths():
    """Adding `docs/context/` to `TRACKER_PATHS` would widen the write scope of
    every task in the registry at once — the S31 refusal the seeded decision
    record carries.

    The CONTENTS of that tuple are pinned at `test_tasks.py:2414`, as an
    equality over all six paths, which is where the registry's own claims
    belong; restating them here would also spell four tracker names in evaluated
    code, which is what makes a file count as a READER of them in
    `validation._files_reading_documents`. What is asserted here is only what
    THIS task could have broken: the list is the same length it was, and nothing
    in it reaches the context tree.
    """
    assert len(tasks.TRACKER_PATHS) == 6
    assert not any(entry.startswith(CONTEXT_DIR) for entry in tasks.TRACKER_PATHS)
    assert not any(entry.startswith("docs/context") for entry in tasks.TRACKER_PATHS)


def test_the_module_reaches_the_registrys_validator_rather_than_defining_one():
    """A source-level claim, because the spy above proves today's call path and
    this proves nobody added a second implementation beside it."""
    source = (REPO_ROOT / "autoloop" / "context_records.py").read_text(encoding="utf-8")
    assert "tasks._validate_approved_path(" in source
    assert "def _validate_approved_path" not in source
    assert "_APPROVED_PATH_SEGMENT_RE" not in source
    assert context_records.tasks is tasks
