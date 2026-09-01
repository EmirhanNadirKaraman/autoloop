"""Project context as records a program can resolve, verify and expire.

ONE SUBJECT: what a file under `docs/context/` must contain, and the mechanical
stamp that says which commit its claims were last checked against. Nothing here
decides what the loop DOES with a record — no packet, no prompt, no task field
reads this module today — because a format nobody can validate is the thing
being replaced, and adding a consumer before the contract holds would repeat it.

WHY NOT ANOTHER MARKDOWN DOCUMENT. This repository already carries a lot of
prose about itself and exactly one document a program could act on:

* `docs/SUMMARY.md` is a file-to-responsibility index with no ids, no status and
  no verification;
* `docs/AUTOLOOP.md` is long-form sections carrying dates but no status, and
  nothing links a task to a section;
* `docs/COMMON_ERRORS.md` is a symptom-first incident log with no ids;
* `docs/SECURITY.md` IS the closest thing that works — stable ids, an explicit
  status, evidence, and a check a reader re-runs.

This module copies the fourth one's discipline and invents no second one. A
record POINTS at the document that holds the detail; it never restates it.

THE ONE VALIDATOR. `load_context_records` is the only way to read the tree, and
it refuses rather than skips. Every Markdown file under `docs/context/` is
either a record it parsed or one of the two STRUCTURAL names (`index.md`,
`README.md`) it reports as structural — and a structural document that opens
with the record fence is refused too, so a malformed record cannot be renamed
into the quiet category. A file that is neither is named and refused.

WHAT "EVERY MARKDOWN FILE" INCLUDES: one whose name begins with a dot. The
classification is SUFFIX-FIRST, not filename-first, and that order is the whole
guard. `.hidden.md` is parsed, id-checked, successor-checked and index-checked
exactly like `features/anything.md`, because an exemption keyed on the first
character would let a malformed or unindexed record leave the one-validator
contract by being renamed — a guard that switches itself off for precisely the
file that was trying to evade it. The suffix match is case-folded for the same
reason: `.hidden.MD` is one keystroke from `.hidden.md` on a case-preserving
filesystem, and it must not be the keystroke that buys the exemption back. The
only thing stepped over is a NON-Markdown dotfile: `.DS_Store`, `.gitkeep` and
the rest of an editor's or the operating system's droppings, which no contract
written for a `.md` record can describe. Those are not dropped in silence
either — the loader returns them as `ContextRepository.ignored` and `check`
prints every one, so a file sitting in this tree that nothing validated is still
said out loud.

A SYMLINK is refused outright, before the name is even looked at, and that is
the same bypass by a different route: a dangling symlink answers False to
`is_file` AND to `is_dir`, so the obvious sweep — every entry that is a regular
file — steps over it without a word. `features/x.md` as a broken link is a
record git can hold and no reader can check. Refusing every symlink rather than
only the broken ones is deliberate: a working one is validated as whatever it
happens to point at TODAY, which is a different file tomorrow. "Loaded zero records"
is never an answer either: a missing `docs/context/` directory and a missing
index are both refusals, because a validator that passes when its input is
absent is the fail-open shape `docs/SECURITY.md` records twice for name-filtered
rechecks.

PATHS ARE VALIDATED BY `tasks._validate_approved_path` ITSELF, called through
the module object rather than copied or re-implemented. A record naming a path
shape the task registry would refuse is a record pointing somewhere the loop
cannot read, and two validators would drift into exactly that — the
one-implementation rule `tasks.deletable_paths` states for scope checking. The
commit shape is `tasks._COMMIT_SHA_RE` for the same reason.

THE STAMP IS A MEASUREMENT, NOT A FIELD SOMEBODY TYPES. `last_verified_commit`
is either the sentinel `UNSTAMPED` or a full sha this repository resolves, and
the only supported way to move it off the sentinel is `stamp_records`, which
reads HEAD through `GitGateway` — the way every other component in this package
asks git. Write-capable agents in this loop have no shell
(`implement_executor.WRITE_ALLOWED_TOOLS` is Read/Grep/Glob/Edit/Write) and are
handed no sha in their prompt, so a sha appearing in a record an agent wrote is
a fabricated measurement. Seed records therefore ship UNSTAMPED and stay that
way until the stamping path has run.

"A SHA THIS REPOSITORY RESOLVES" IS ASKED OF GIT, NOT OF A REGULAR EXPRESSION.
The shape check (`tasks._COMMIT_SHA_RE`) only says the value could be a sha;
`abcd...` forty times over passes it and names nothing. So `load_context_records`
— the one mandatory path into this tree, the one `stamp_records` and the entry
point both go through — puts every non-sentinel value to git through
`GitGateway`, and REFUSES the record by name when git says the object database
does not hold it (`unknown_commit`) or cannot answer at all
(`unresolvable_commit`). The fail direction is the opposite of
`cli._candidate_is_retired`'s, which returns "no answer" as a quiet no: a
validator that accepted a stamp because git was unavailable would be the
alarm-never-fires shape twice over, so an unanswerable question raises here.

The gateway is built only when at least one record is stamped. That is not the
guard switching itself off when its input is absent: the set it guards is
`[record for record in records if record.stamped]`, and EVERY member of that set
reaches git or the load raises. An all-UNSTAMPED tree — which is what this
repository's seeds are until `stamp` runs — has nothing to resolve, so it asks
nothing, and no unverified sha exists for it to have missed.

STAMPING VERIFIES BEFORE IT WRITES, AND VERIFIES AGAINST THE COMMIT IT IS ABOUT.
Every source and test path a pending record names must be in the TREE of the
commit being stamped — enumerated with `ls-tree -r` through the same gateway —
or nothing is stamped at all. The WORKING TREE cannot answer that question:
`Path.exists` is equally True for a file that is untracked, one staged but never
committed, and one deleted from HEAD and restored on disk, and stamping any of
those writes HEAD into a record as though that commit held evidence it does not.
The other way to close it — refuse to stamp unless the worktree matches HEAD —
is not available to a path whose whole job is to WRITE records into the worktree:
the first record it stamped would forbid the second. So the question asked is the
exact one the stamp claims to have answered, and a stamp whose paths were never
checked at that commit would be decoration on a claim, which is the placeholder
failure this format exists to make impossible.

Stamping touches only records still carrying the sentinel. That is what makes it
re-runnable — a second run writes nothing — and it is also the honest rule: a
stamp says "these claims were checked at this commit", so moving it forward
because HEAD moved would assert a verification nobody performed. Re-verifying is
an edit: put the sentinel back and run the stamp again.

A RECORD IS REPLACED, NEVER OVERWRITTEN IN PLACE. `Path.write_text` truncates
the target before it writes, so an I/O failure part-way through leaves a file
that is neither the old record nor the new one; building the whole string in
memory first does not help, because the truncation happens on the filesystem
after the string is complete. `_write_atomically` renames a finished temp file
over the target instead, so each record is the text it had before the run or the
text the run built and never a truncation of either. What that does NOT make
atomic is the SET: a failure part-way down the list leaves earlier records
stamped and later ones on the sentinel, both whole and both loadable, and the
next run stamps the remainder. Nor is the read-back after those writes free of
refusals — `stamp_records` says exactly which of its steps can raise with files
already changed, because "every refusal happens before the first write" was the
convenient version of that sentence rather than the true one.

Entry point: `python3 -m autoloop.context_records [check|stamp] [root]`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import tasks
from .errors import AutoloopError, GitError, TaskGraphError
from .git_gateway import GitGateway
from .policy import PolicyConfig, PolicyEngine

#: Where the records live, repository-relative. A directory of its own rather
#: than more rows in an existing tracker: these files are parsed, and mixing
#: parsed records into a prose document would make every prose edit a parse
#: risk. NOT added to `tasks.TRACKER_PATHS` — that would widen the write scope
#: of every task in the registry at once, which `docs/SECURITY.md` S31 refused
#: for `[repo].tracker_paths`.
CONTEXT_DIR = "docs/context"

#: The index every record must appear in, at the root of `CONTEXT_DIR`.
INDEX_NAME = "index.md"

#: Files under `CONTEXT_DIR` that are navigation rather than records. Both are
#: checked for the record fence anyway (see `_load_structural`), so this is a
#: list of names that may be prose, not a list of files nobody validates.
#: Matched EXACTLY, unlike the suffix: `README.MD` and `.README.md` are not
#: these names, so each is a record and is parsed. That direction is the safe
#: one — a near-miss spelling falls INTO the contract rather than out of it.
STRUCTURAL_NAMES = frozenset({INDEX_NAME, "README.md"})

#: What makes a file under `CONTEXT_DIR` this contract's business. Matched
#: against the whole NAME rather than through `Path.suffix`, because `Path`
#: reads a leading dot as the start of a stem: `Path(".md").suffix` is `''` and
#: `Path(".hidden.md").suffix` is `'.md'`, so a suffix test alone would let a
#: file literally named `.md` through as an ignorable dotfile. Case-FOLDED
#: before the comparison, because the filesystems this runs on are not: on a
#: case-preserving one `.hidden.MD` is as easy to write as `.hidden.md`, and an
#: exact-case test would hand the second spelling the exemption the first was
#: just denied.
RECORD_SUFFIX = ".md"

#: Opens and closes the metadata block. Three hyphens, alone on the line: the
#: shape a reader already expects at the top of a document, and one that cannot
#: be confused with a Markdown heading.
FENCE = "---"

#: `last_verified_commit` when nothing has verified the record yet. UPPERCASE
#: so it can never be mistaken for a sha (`tasks._COMMIT_SHA_RE` accepts
#: lowercase hex only) and so a reader scanning a record sees the gap.
UNSTAMPED = "UNSTAMPED"

#: What a record is ABOUT. Closed set: a kind nobody validated is a kind nobody
#: can search on, and the per-kind section requirements below hang off it.
KINDS = ("project", "architecture", "feature", "incident", "decision", "lesson")

#: Where a record is in its life. `active` is current; `resolved` describes
#: something that happened and is finished (an incident); `superseded` MUST name
#: a successor; `retired` is context that stopped applying and names nothing.
STATUSES = ("active", "resolved", "superseded", "retired")

#: Every field is REQUIRED to be present, including the ones that are often
#: empty (`test_paths`, `task_ids`, `superseded_by`). An omitted field and an
#: empty one look identical afterwards, and only one of them was a decision.
FIELDS = (
    "id",
    "kind",
    "status",
    "summary",
    "source_paths",
    "test_paths",
    "task_ids",
    "last_verified_commit",
    "superseded_by",
)

#: Fields whose value is a whitespace-separated list. Whitespace is unambiguous
#: here BECAUSE `tasks._validate_approved_path` refuses a path segment
#: containing any, and `tasks._ID_RE` refuses an id containing any — so one line
#: can hold a list without a nested syntax to get wrong.
LIST_FIELDS = frozenset({"source_paths", "test_paths", "task_ids"})

#: A summary is one line and is meant to fit an index row.
MAX_SUMMARY_CHARS = 200

#: Headings a record of this kind must carry, spelled exactly as `## <heading>`.
#: Only the three kinds whose contents are specified appear here: a FEATURE
#: carries intent, entry points, invariants, data flow, the tests and decisions
#: that bind it and its known failure modes; an INCIDENT carries symptom,
#: reproduction, root cause, resolution, regression test and affected commits; a
#: LESSON carries the evidence that it happened more than once and one concrete
#: prevention rule. `project`, `architecture` and `decision` are held to a
#: non-empty body only — their shape is not specified anywhere yet, and inventing
#: one here would be this module deciding a documentation question on its own.
REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    "feature": (
        "Intent and boundaries",
        "Entry points",
        "Invariants",
        "Data flow",
        "Tests and decisions",
        "Known failure modes",
    ),
    "incident": (
        "Symptom",
        "Reproduction",
        "Root cause",
        "Resolution",
        "Regression test",
        "Affected commits",
    ),
    "lesson": (
        "Evidence",
        "Prevention rule",
    ),
}


class ContextRecordError(AutoloopError):
    """A context record, or the tree of them, was REFUSED.

    Carries a stable `code` in the same shape as `TaskGraphError`, and a message
    that names the record — by id when the header parsed far enough to have one,
    by path when it did not — followed by the reason. Both halves are the point:
    "a record is malformed" sends a reader through the whole directory, and a
    reason without a name sends them to the wrong file.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ContextRecord:
    """One validated record.

    `path` is repository-relative and is the record's location, not a claim
    about it. `body` is everything after the metadata block, kept so a caller
    can render a record without re-reading the file, and checked for the
    headings its kind requires.

    `commit_line` is the index, within the file's lines, of the
    `last_verified_commit` line — the one line `stamp_records` rewrites. It is
    recorded HERE, by the parser that proved the line is inside the metadata
    block, so stamping never has to search the file for a key a body could also
    spell at the start of a line.
    """

    path: str
    id: str
    kind: str
    status: str
    summary: str
    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    task_ids: tuple[str, ...]
    last_verified_commit: str
    superseded_by: str
    body: str
    commit_line: int

    @property
    def stamped(self) -> bool:
        """Has anything verified this record against a commit?"""
        return self.last_verified_commit != UNSTAMPED

    @property
    def referenced_paths(self) -> tuple[str, ...]:
        """Every repository path this record points at, sources then tests."""
        return (*self.source_paths, *self.test_paths)


@dataclass(frozen=True)
class ContextRepository:
    """Everything under `CONTEXT_DIR`, after one validation pass.

    `structural` and `ignored` are RETURNED rather than dropped: a loader that
    silently passed over a file would be indistinguishable from one that never
    saw it, which is the failure this format exists to refuse. `structural` is
    the navigation documents it recognised by name; `ignored` is NON-Markdown
    dotfiles (an editor's or the operating system's droppings), which no
    contract written for a `.md` record can describe. A dotfile that IS Markdown
    is a record and appears in `records` or stops the load — the dot buys no
    exemption.
    """

    root: Path
    records: tuple[ContextRecord, ...]
    structural: tuple[str, ...]
    ignored: tuple[str, ...] = ()

    def by_id(self) -> dict[str, ContextRecord]:
        return {record.id: record for record in self.records}


@dataclass(frozen=True)
class Stamp:
    """What one `stamp_records` call actually wrote.

    `head_sha` is the commit read from git for this run — always reported, even
    when nothing was stamped, so a caller can tell "already current" from "could
    not resolve HEAD" (the second raises and returns no `Stamp` at all).
    `stamped` are the ids moved off the sentinel by THIS call, `already` the ids
    that were carrying a commit before it and were left alone.
    """

    head_sha: str
    stamped: tuple[str, ...] = ()
    already: tuple[str, ...] = ()


def _label(path: str, record_id: str = "") -> str:
    return f"context record '{record_id}' ({path})" if record_id else f"context record {path}"


def _split_front_matter(text: str, path: str) -> tuple[list[str], int]:
    """`(lines, index of the closing fence)`, or raise.

    Split on `\\n` and never re-joined by anything but `\\n`, so a rewritten
    line leaves every other byte of the file exactly where it was.
    """
    if not text.strip():
        raise ContextRecordError("empty_record", f"{_label(path)} is empty")
    if "\r" in text:
        raise ContextRecordError(
            "carriage_return",
            f"{_label(path)} contains a carriage return — records are LF-only so a "
            "line rewritten by the stamping path cannot change the file's line endings",
        )
    lines = text.split("\n")
    if lines[0] != FENCE:
        raise ContextRecordError(
            "no_front_matter",
            f"{_label(path)} must open with a metadata block: line 1 has to be "
            f"exactly {FENCE!r}, not {lines[0]!r}",
        )
    for index in range(1, len(lines)):
        if lines[index] == FENCE:
            return lines, index
    raise ContextRecordError(
        "unterminated_front_matter",
        f"{_label(path)} opens a metadata block that is never closed by a {FENCE!r} line",
    )


def _parse_fields(lines: list[str], close: int, path: str) -> tuple[dict[str, str], int]:
    """`({field: value}, index of the last_verified_commit line)`, or raise.

    Strict by construction: one `key: value` per line, no leading whitespace, no
    padding around the value, no unknown key, no duplicate key, no missing key.
    Every one of those is refused by NAME — a parser that guessed at any of them
    would be deciding what a record says on the author's behalf.
    """
    values: dict[str, str] = {}
    commit_line = -1
    for index in range(1, close):
        line = lines[index]
        if not line.strip():
            raise ContextRecordError(
                "blank_field_line",
                f"{_label(path)} has a blank line inside its metadata block (line {index + 1})",
            )
        if line != line.lstrip():
            raise ContextRecordError(
                "indented_field",
                f"{_label(path)} indents metadata line {index + 1} ({line!r}); every field "
                "starts at column 1",
            )
        key, sep, rest = line.partition(":")
        if not sep:
            raise ContextRecordError(
                "bad_field_line",
                f"{_label(path)} metadata line {index + 1} ({line!r}) is not 'key: value'",
            )
        if key not in FIELDS:
            raise ContextRecordError(
                "unknown_field",
                f"{_label(path)} names unknown metadata field {key!r} — the fields are "
                f"{', '.join(FIELDS)}",
            )
        if key in values:
            raise ContextRecordError(
                "duplicate_field",
                f"{_label(path)} names metadata field {key!r} more than once; which value "
                "wins is not a question this parser answers",
            )
        if rest and not rest.startswith(" "):
            raise ContextRecordError(
                "bad_field_line",
                f"{_label(path)} metadata line {index + 1} ({line!r}) needs one space after "
                "the colon",
            )
        value = rest[1:] if rest else ""
        if value != value.strip():
            raise ContextRecordError(
                "padded_field",
                f"{_label(path)} pads the value of {key!r} with whitespace ({value!r})",
            )
        values[key] = value
        if key == "last_verified_commit":
            commit_line = index
    missing = [field for field in FIELDS if field not in values]
    if missing:
        raise ContextRecordError(
            "missing_field",
            f"{_label(path)} is missing required metadata field(s): {', '.join(missing)}",
        )
    return values, commit_line


def _validate_list(field: str, raw: str, path: str, record_id: str) -> tuple[str, ...]:
    """A whitespace-separated list field, every entry validated and unique.

    Path entries go through `tasks._validate_approved_path` — the registry's own
    function, reached through the module object so a test can prove the call
    happens rather than prove two implementations agree today. Its refusal is
    re-raised with the record named and the registry's reason kept verbatim.
    """
    entries = raw.split()
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            raise ContextRecordError(
                f"duplicate_{field}",
                f"{_label(path, record_id)} names {entry!r} more than once in {field}",
            )
        seen.add(entry)
        if field == "task_ids":
            if not tasks._ID_RE.match(entry):
                raise ContextRecordError(
                    "bad_task_id",
                    f"{_label(path, record_id)} names {entry!r} in task_ids, which is not a "
                    "task id shape the registry accepts",
                )
            continue
        try:
            tasks._validate_approved_path(entry)
        except TaskGraphError as exc:
            raise ContextRecordError(
                f"bad_{field[:-1]}",
                f"{_label(path, record_id)} names {entry!r} in {field}, which the task "
                f"registry itself refuses: {exc}",
            ) from exc
    return tuple(entries)


def _check_sections(kind: str, body: str, path: str, record_id: str) -> None:
    if not body.strip():
        raise ContextRecordError(
            "empty_body",
            f"{_label(path, record_id)} has metadata and no body — a record with nothing "
            "under its headings is the placeholder this format exists to refuse",
        )
    headings = {line[3:].strip() for line in body.split("\n") if line.startswith("## ")}
    for required in REQUIRED_SECTIONS.get(kind, ()):
        if required not in headings:
            raise ContextRecordError(
                "missing_section",
                f"{_label(path, record_id)} is kind {kind!r} and must carry a "
                f"'## {required}' section",
            )


def parse_record(text: str, path: str) -> ContextRecord:
    """THE record parser. Returns a validated `ContextRecord` or raises
    `ContextRecordError` naming the record and the reason.

    Every refusal below is a shape a reader would otherwise have to notice by
    eye. What this function does NOT check is anything needing the repository or
    the other records: whether a named path exists (`missing_paths`), whether a
    successor resolves, whether the index lists it, or whether the commit is one
    git knows (`load_context_records` and `stamp_records`, which have a root and
    a gateway respectively).
    """
    lines, close = _split_front_matter(text, path)
    values, commit_line = _parse_fields(lines, close, path)

    record_id = values["id"]
    if not tasks._ID_RE.match(record_id):
        raise ContextRecordError(
            "bad_record_id",
            f"{_label(path)} has id {record_id!r}, which is not a stable id shape "
            "(alphanumeric start, then [A-Za-z0-9._-], at most 64 characters)",
        )

    kind = values["kind"]
    if kind not in KINDS:
        raise ContextRecordError(
            "bad_kind",
            f"{_label(path, record_id)} has kind {kind!r}; the kinds are {', '.join(KINDS)}",
        )

    status = values["status"]
    if status not in STATUSES:
        raise ContextRecordError(
            "bad_status",
            f"{_label(path, record_id)} has status {status!r}; the statuses are "
            f"{', '.join(STATUSES)}",
        )

    summary = values["summary"]
    if not summary:
        raise ContextRecordError(
            "empty_summary", f"{_label(path, record_id)} has no summary"
        )
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ContextRecordError(
            "long_summary",
            f"{_label(path, record_id)} has a {len(summary)}-character summary; a summary is "
            f"one line of at most {MAX_SUMMARY_CHARS}",
        )

    source_paths = _validate_list("source_paths", values["source_paths"], path, record_id)
    if not source_paths:
        raise ContextRecordError(
            "no_source_paths",
            f"{_label(path, record_id)} names no source_paths — a record that points at "
            "nothing cannot be verified against any commit",
        )
    test_paths = _validate_list("test_paths", values["test_paths"], path, record_id)
    if kind == "feature" and not test_paths:
        raise ContextRecordError(
            "no_test_paths",
            f"{_label(path, record_id)} is a feature record and must name the tests that "
            "bind it in test_paths",
        )
    task_ids = _validate_list("task_ids", values["task_ids"], path, record_id)
    if kind in ("incident", "lesson") and not task_ids:
        raise ContextRecordError(
            "no_task_ids",
            f"{_label(path, record_id)} is kind {kind!r} and must name in task_ids the "
            "task(s) it is evidence from",
        )

    commit = values["last_verified_commit"]
    if commit != UNSTAMPED and not tasks._COMMIT_SHA_RE.match(commit):
        raise ContextRecordError(
            "bad_last_verified_commit",
            f"{_label(path, record_id)} has last_verified_commit {commit!r}, which is "
            f"neither {UNSTAMPED} nor a full lowercase commit sha (40 or 64 hex "
            "characters) — the value is written by stamp_records, never by hand",
        )

    successor = values["superseded_by"]
    if successor.split() != ([successor] if successor else []):
        raise ContextRecordError(
            "bad_successor",
            f"{_label(path, record_id)} has superseded_by {successor!r}; a record is "
            "superseded by exactly one successor, or by none",
        )
    if successor and not tasks._ID_RE.match(successor):
        raise ContextRecordError(
            "bad_successor",
            f"{_label(path, record_id)} names successor {successor!r}, which is not a "
            "record id shape",
        )
    if status == "superseded" and not successor:
        raise ContextRecordError(
            "missing_successor",
            f"{_label(path, record_id)} has status 'superseded' and names no successor — "
            "the successor IS what makes the status readable",
        )
    if status != "superseded" and successor:
        raise ContextRecordError(
            "unexpected_successor",
            f"{_label(path, record_id)} names successor {successor!r} while its status is "
            f"{status!r}; only a superseded record has one",
        )
    if successor == record_id:
        raise ContextRecordError(
            "self_supersession",
            f"{_label(path, record_id)} names itself as its own successor",
        )

    body = "\n".join(lines[close + 1:])
    _check_sections(kind, body, path, record_id)

    return ContextRecord(
        path=path,
        id=record_id,
        kind=kind,
        status=status,
        summary=summary,
        source_paths=source_paths,
        test_paths=test_paths,
        task_ids=task_ids,
        last_verified_commit=commit,
        superseded_by=successor,
        body=body,
        commit_line=commit_line,
    )


def _read(file: Path, rel: str) -> str:
    try:
        return file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ContextRecordError(
            "undecodable_record", f"{_label(rel)} is not valid UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        raise ContextRecordError("unreadable_record", f"{_label(rel)} cannot be read: {exc}") from exc


def _check_successors(records: tuple[ContextRecord, ...]) -> None:
    """Every named successor resolves, and no chain of them is a loop."""
    known = {record.id: record for record in records}
    for record in records:
        if record.superseded_by and record.superseded_by not in known:
            raise ContextRecordError(
                "unknown_successor",
                f"{_label(record.path, record.id)} names successor "
                f"'{record.superseded_by}', which is not a record in {CONTEXT_DIR}",
            )
    for record in records:
        seen = {record.id}
        walker = record
        while walker.superseded_by:
            walker = known[walker.superseded_by]
            if walker.id in seen:
                raise ContextRecordError(
                    "successor_cycle",
                    f"{_label(record.path, record.id)} is superseded through a cycle that "
                    f"returns to '{walker.id}' — no record in it is the current one",
                )
            seen.add(walker.id)


def _check_index(root: Path, records: tuple[ContextRecord, ...]) -> None:
    """`index.md` must name every record, by id AND by its path.

    Forward only: a row for a record that does not exist is left to a reader,
    because an index entry naming nothing is a dead link while a record missing
    from the index is a record nobody finds. The check is a substring test, so
    the index is free to be a table, a list, or prose.
    """
    index = root / CONTEXT_DIR / INDEX_NAME
    if not index.is_file():
        raise ContextRecordError(
            "missing_index",
            f"{CONTEXT_DIR}/{INDEX_NAME} does not exist — with no index there is nothing "
            "that lists the records, and a record nobody can find is not context",
        )
    text = _read(index, f"{CONTEXT_DIR}/{INDEX_NAME}")
    for record in records:
        relative = record.path[len(CONTEXT_DIR) + 1:]
        if record.id not in text or relative not in text:
            raise ContextRecordError(
                "unindexed_record",
                f"{_label(record.path, record.id)} is not listed in {CONTEXT_DIR}/"
                f"{INDEX_NAME} by both its id and its path {relative!r}",
            )


def _load_structural(file: Path, rel: str) -> None:
    """A navigation document is still read, and refused if it opens with the
    record fence. Without this, renaming a broken record to `README.md` would
    move it into the category nothing validates — a guard that switches itself
    off for the file that wants it most."""
    if _read(file, rel).startswith(FENCE):
        raise ContextRecordError(
            "record_in_structural_file",
            f"{rel} opens with a {FENCE!r} metadata block; {', '.join(sorted(STRUCTURAL_NAMES))} "
            "are navigation documents and may not carry a record header",
        )


def _gateway(root: Path, git=None) -> GitGateway:
    """`GitGateway(root, PolicyEngine(PolicyConfig()))` unless a caller passed
    one — the construction `cli.repo_fingerprint` already makes for a read-only
    question about the checkout it is standing in."""
    return git if git is not None else GitGateway(root, PolicyEngine(PolicyConfig()))


def _verify_commit(git, oid: str, prefix: str) -> str:
    """Refuse unless git itself resolves `oid` to a commit in this repository,
    and return that commit's TREE id.

    The tree comes back because the caller that needs it — `stamp_records`, which
    has to ask what the commit CONTAINS — would otherwise spend a second
    subprocess (`rev-parse <sha>^{tree}`) re-deriving a value this function
    already read out of the commit object. Worse than the cost: `rev-parse` dies
    with status 128 for a missing object exactly as it does for an unreadable
    one, which is the "died is not an answer" ambiguity the two probes below
    exist to avoid, so re-asking would reintroduce it after it had been settled.

    Two probes, in the order `cli._candidate_is_retired` and
    `orchestrator._commit_presence` already use — and for the same reason.
    `cat-file commit` (`read_commit`) dies with the same status for a missing
    object, a blob wearing a commit's name, a corrupt object, an I/O error and a
    policy refusal, so its failure alone proves only that the question went
    unanswered. `object_exists` is the one probe whose EXIT CODE carries the
    distinction (0 present, 1 absent, anything else raises), so a raise from the
    first leads to one more question rather than to a verdict.

    What an unanswered question AUTHORIZES is what differs, deliberately. Both
    of those callers refuse to act on one and carry on: `_candidate_is_retired`
    returns `""` — "still respect this candidate" — and `_commit_presence`
    returns `None`, which authorizes nothing and leaves the record alone. In
    both, silence withholds a destructive act. Here the act being authorized is
    the opposite one — ACCEPTING a stamp — so silence must not withhold the
    refusal instead: a stamp claims somebody checked these pointers at a named
    commit, and passing it because the repository was unreadable would let the
    claim through precisely when nothing could check it. Hence a raise.

    `prefix` names whose commit this is and quotes the value, so the caller's
    subject survives into the message: a reason with no name sends a reader
    through the whole tree.
    """
    detail = ""
    try:
        info = git.read_commit(oid)
        tree = info.get("tree", "")
        if tree:
            return tree
        detail = "git returned no tree header for it, so it is not a commit object"
    except (GitError, OSError) as exc:
        detail = str(exc)
    try:
        present = git.object_exists(oid)
    except (GitError, OSError) as exc:
        raise ContextRecordError(
            "unresolvable_commit",
            f"{prefix}, and git could not answer whether this repository holds it "
            f"({exc}) — an unanswerable question is not a pass, so this is refused "
            "rather than accepted on git's silence",
        ) from exc
    if not present:
        raise ContextRecordError(
            "unknown_commit",
            f"{prefix}, which git says this repository's object database does not "
            "hold; the value is written by stamp_records from a resolved HEAD, never "
            "by hand, and a sha nothing resolves is evidence of nothing",
        )
    raise ContextRecordError(
        "unresolvable_commit",
        f"{prefix}, and the object exists here but git could not read it as a "
        f"commit ({detail})",
    )


def _verify_commits(root: Path, records: tuple[ContextRecord, ...], git=None) -> None:
    """Put every stamped record's commit to git; UNSTAMPED records ask nothing.

    The gateway is constructed only when something needs resolving — not a guard
    that switches itself off, because the set it guards is exactly the stamped
    records and every one of them is asked about. A tree in which nothing claims
    a commit has no unverified sha in it to miss.
    """
    stamped = [record for record in records if record.stamped]
    if not stamped:
        return
    gateway = _gateway(root, git)
    for record in stamped:
        _verify_commit(
            gateway,
            record.last_verified_commit,
            f"{_label(record.path, record.id)} names last_verified_commit "
            f"'{record.last_verified_commit}'",
        )


def load_context_records(root, git=None) -> ContextRepository:
    """THE loader for `docs/context/` — every file under it, one validation pass.

    Refuses, never skips. Classification is by SUFFIX first: every Markdown
    file is a record or a structural document, `.hidden.md` included, so no
    rename moves a file out of the contract. Only a non-Markdown dotfile is
    stepped over, and it is reported rather than dropped. A symlink is refused
    before any of that, because a dangling one is neither a file nor a
    directory and a sweep filtered to regular files would drop it in silence.
    Absent input is a
    refusal too: no `docs/context/` directory and no `index.md` each raise,
    because "validated zero records successfully" is a pass nobody asked for.

    Checks run cheapest-first — parse, ids, successors, index, and git last —
    so a tree that fails on shape never reaches a subprocess. The last of them
    is the one that makes a stamp mean something: every `last_verified_commit`
    that is not the sentinel is put to git through `GitGateway` and refused by
    name unless this repository resolves it to a commit. `git` is for a caller
    that already has a gateway (`stamp_records` passes its own so one run asks
    one repository); left None, one is built only if a record is stamped.

    Ordering is by repository-relative path, so two runs over the same tree
    return the same tuple and a caller may compare them.
    """
    root = Path(root)
    base = root / CONTEXT_DIR
    if not base.is_dir():
        raise ContextRecordError(
            "missing_context_dir",
            f"{CONTEXT_DIR} is not a directory under {root} (absent, or something else) — "
            "there is no context tree to load",
        )

    records: list[ContextRecord] = []
    structural: list[str] = []
    ignored: list[str] = []
    for file in sorted(base.rglob("*")):
        rel = file.relative_to(root).as_posix()
        if file.is_symlink():
            raise ContextRecordError(
                "symlinked_entry",
                f"{rel} under {CONTEXT_DIR} is a symlink; this tree holds regular files and "
                "real directories. A DANGLING one is the reason this is a refusal rather "
                "than a preference: it is neither a file nor a directory to `Path`, so a "
                "sweep that kept only regular files would step over it in silence — a "
                "record that evades the contract by being a broken link",
            )
        if file.is_dir():
            continue
        if not file.is_file():
            raise ContextRecordError(
                "irregular_entry",
                f"{rel} under {CONTEXT_DIR} is neither a regular file nor a directory; "
                "nothing here can say what it holds, and an entry nothing can read is not "
                "stepped over",
            )
        if not file.name.lower().endswith(RECORD_SUFFIX):
            # A NON-Markdown dotfile is the only thing stepped over, and it is
            # reported. Every other non-record file is refused by name.
            if file.name.startswith("."):
                ignored.append(rel)
                continue
            raise ContextRecordError(
                "foreign_file",
                f"{rel} is under {CONTEXT_DIR} and is not a {RECORD_SUFFIX} file; this tree "
                "holds records and the documents that index them, nothing else",
            )
        if file.name in STRUCTURAL_NAMES:
            _load_structural(file, rel)
            structural.append(rel)
            continue
        records.append(parse_record(_read(file, rel), rel))

    by_id: dict[str, str] = {}
    for record in records:
        if record.id in by_id:
            raise ContextRecordError(
                "duplicate_record_id",
                f"{_label(record.path, record.id)} reuses the id of {by_id[record.id]} — an "
                "id that names two records resolves to neither",
            )
        by_id[record.id] = record.path

    frozen = tuple(records)
    _check_successors(frozen)
    _check_index(root, frozen)
    _verify_commits(root, frozen, git)
    return ContextRepository(
        root=root, records=frozen, structural=tuple(structural), ignored=tuple(ignored)
    )


def missing_paths(record: ContextRecord, root) -> tuple[str, ...]:
    """The paths this record names that are not in the CHECKOUT at `root`.

    Empty means every pointer resolves in the tree as it stands right now. That
    is the question `check` asks — "has a pointer moved since anyone looked" —
    and it is deliberately NOT the question stamping asks. A stamp names a
    COMMIT, and this function cannot see one: it answers True for an untracked
    file, for one staged and never committed, and for one deleted from HEAD and
    restored on disk. `missing_paths_in_tree` is the commit-shaped question, and
    `stamp_records` uses that one.
    """
    root = Path(root)
    return tuple(rel for rel in record.referenced_paths if not (root / rel).exists())


def commit_tree_paths(git, tree: str, subject: str) -> frozenset[str]:
    """Every path the tree object `tree` holds: the blobs `ls-tree -r` lists,
    plus every directory that is an ancestor of one.

    The ancestors are synthesised HERE because `ls-tree -r` emits no directory
    entries at all, while `tasks._validate_approved_path` accepts a trailing
    slash as "everything under here" — so a record may legitimately name
    `autoloop/tests/`, and a membership test over blobs alone would refuse it as
    absent from a commit that plainly contains it.

    Git failure RAISES rather than returning what it managed to read. An empty
    or partial set would make every pointer look missing, which is loud rather
    than silent — but it is loud about the wrong thing, and the honest statement
    of "the repository could not tell us what this commit contains" is a refusal
    naming the tree, not a per-record complaint about paths nobody checked.
    """
    try:
        entries = git.tree_entries(tree)
    except (GitError, OSError) as exc:
        raise ContextRecordError(
            "unreadable_tree",
            f"{subject}, and git could not list what its tree '{tree}' contains "
            f"({exc}) — nothing is stamped against a commit whose contents could not "
            "be read, because a tree nobody could enumerate would otherwise verify "
            "every pointer by default",
        ) from exc
    paths = set(entries)
    for entry in entries:
        segments = entry.split("/")
        for depth in range(1, len(segments)):
            paths.add("/".join(segments[:depth]))
    return frozenset(paths)


def missing_paths_in_tree(record: ContextRecord, tree_paths: frozenset[str]) -> tuple[str, ...]:
    """The paths this record names that a commit's tree does not hold.

    `tree_paths` comes from `commit_tree_paths`, so directories are already in
    it. The trailing slash a record may write to mark a directory is stripped
    for the comparison, exactly as `tasks._validate_approved_path` strips it
    before checking segments — the same normalisation, so the two agree on what
    a path is.
    """
    return tuple(rel for rel in record.referenced_paths if rel.rstrip("/") not in tree_paths)


def unverifiable_records(repository: ContextRepository) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """`((record id, missing paths), ...)` for every record with a broken
    pointer — the whole list, not the first, so one run names everything to fix."""
    broken = []
    for record in repository.records:
        gone = missing_paths(record, repository.root)
        if gone:
            broken.append((record.id, gone))
    return tuple(broken)


def _temp_for(file: Path) -> Path:
    """Where a record's new text is built before it replaces `file`.

    The SAME DIRECTORY, so `os.replace` stays inside one filesystem and is the
    atomic rename it advertises rather than a copy. The name is deliberately both
    DOTTED and NON-Markdown: a run killed between creating this file and renaming
    it leaves the temp behind, and that leftover then falls into the one category
    `load_context_records` steps over and REPORTS (`ContextRepository.ignored`)
    instead of the category it refuses the whole tree for. `project.md.tmp` would
    be a `foreign_file` and a crashed stamp would leave the context tree
    unloadable until somebody deleted it by hand; `.project.md.<pid>.stamp-tmp`
    is named out loud by `check` and blocks nothing. The pid keeps two concurrent
    runs off each other's temp file.
    """
    return file.with_name(f".{file.name}.{os.getpid()}.stamp-tmp")


def _write_atomically(file: Path, text: str) -> None:
    """Replace `file` with `text` in one step, or leave it exactly as it was.

    `Path.write_text` TRUNCATES the target and then writes into it, so a failure
    part-way through — a full disk, a read-only tree, an I/O error — leaves a
    record that is neither the old one nor the new one, commonly one whose
    metadata block no longer parses. Building the string in memory first does not
    prevent that: the truncation happens on the filesystem, after the string is
    already complete. So the new text goes to a temp file beside the target, is
    flushed and fsync'd there — which is where a deferred write error surfaces —
    and only then renamed over the target. A reader sees the record as it was
    before this run or as this run wrote it, never during.

    NOT fsync'd through to the containing directory entry, unlike
    `worktask._atomic_write_json`, and that difference is a decision rather than
    an omission: the marker that writer persists exists to survive a power loss,
    while a stamp lost to one leaves the record exactly where it started, on the
    sentinel, and the next `stamp_records` writes it again. The property that has
    to hold here is that no record is ever half-written, and the rename is what
    buys it.

    `newline=""` passes the string's own line endings through untranslated, so
    the LF-only rule `_split_front_matter` enforces does not depend on the
    platform this runs on.

    Raises `OSError`. The caller turns that into a refusal that names the record
    and says how much of the run had already landed.
    """
    tmp = _temp_for(file)
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, file)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            # The temp file is being cleaned up on the way out of a failure that
            # is about to be raised; losing this one too would replace the real
            # reason with a second-order one. A leftover is reported by the
            # loader (see `_temp_for`), not fatal.
            pass
        raise


def stamp_records(root, git=None) -> Stamp:
    """Move every UNSTAMPED record to the commit this repository's HEAD names.

    THE stamping path, and the only supported writer of `last_verified_commit`.
    Order matters and each step is load-bearing:

    1. load the whole tree, so a malformed record — or one already carrying a
       commit this repository cannot resolve — stops the run before anything is
       written;
    2. resolve HEAD through `GitGateway`, refuse a value that is not a full sha,
       and then put that sha to git like any other: an answer this repository
       cannot resolve back to a commit must never be written INTO a record,
       where the next load would refuse it after the file had already changed;
    3. enumerate what that COMMIT contains and check every pending record's
       pointers against it — all of them, before any write — refusing if any is
       missing. Against the commit's tree and not against the checkout, because
       the sha about to be written is the commit's, and `Path.exists` cannot
       tell a file that commit holds from one that is untracked, staged but
       uncommitted, or deleted in HEAD and restored on disk;
    4. re-read each pending file and confirm the line the parser located still
       reads the sentinel — ALL of them, still before any write, so a file that
       changed underneath the run cannot be discovered halfway through and leave
       some records stamped and some not;
    5. rewrite exactly one line per record, the one the parser located, each file
       replaced through `_write_atomically`;
    6. re-load the tree and confirm each stamped record now reads back at that
       commit, so the run's report is a measurement of the file rather than of
       the intention.

    WHAT HOLDS WHEN THIS RAISES, per step, because the shorter version of this
    paragraph — "every refusal happens before the first write" — was false:

    * **Steps 1-4 write nothing.** Every contract refusal lives here and leaves
      the tree exactly as the run found it: a malformed record or one stamped to
      a commit nothing resolves (`load_context_records`), a HEAD that is not a
      full sha (`head_unresolved`) or one no object resolves (`unknown_commit`,
      `unresolvable_commit`), a tree git cannot list (`unreadable_tree`), a
      pointer that commit does not hold (`unverifiable_record`), and a file that
      changed under the run (`record_changed_under_stamp`). One refusal in this
      range is not a `ContextRecordError` at all: `gateway.head_sha()` raises
      `GitError` straight through. It is still before any write.
    * **Step 5 is atomic per FILE, not across the set.** `_write_atomically`
      renames a finished temp file over each record, so a record is its
      pre-run text or its stamped text and never a truncation of either. A write
      that fails part-way down the list raises `unwritable_record`, naming the
      record and how many were stamped before it; those earlier records carry the
      sha, the rest still carry the sentinel, every one of them is a whole record
      the loader still accepts, and a re-run stamps what is left.
    * **Step 6 runs after those writes are on disk**, and it is a FULL load, so
      it can raise anything the loader can while the files have already changed:
      git becoming unavailable between the write and the read-back
      (`unresolvable_commit`), a record that turned unreadable
      (`unreadable_record`, `undecodable_record`), anything else that changed in
      the tree meanwhile (`foreign_file`, `symlinked_entry`, `unindexed_record`,
      `duplicate_record_id` and the rest), a record that disappeared
      (`stamp_vanished`), and the read-back mismatch the step exists for
      (`stamp_not_written`). It is deliberately NOT narrowed to re-reading the
      files this run wrote: a stamp only its own writer would accept is not a
      measurement. What holds instead is what step 5 guarantees — every file
      written is a whole record — plus idempotence: run it again against a
      healthy repository and it writes nothing, reporting those records as
      `already`.

    One gateway for the whole run, passed into both loads, so every question
    this run asks is asked of one repository.

    Re-runnable: a record already carrying a commit is left alone, so a second
    run over an unchanged tree writes nothing and reports it as `already`. When
    NOTHING is pending the commit's tree is never enumerated — not a check
    switching itself off, because the set it guards is exactly the pending
    records and every member of a non-empty one is checked or the run raises.
    """
    root = Path(root)
    gateway = _gateway(root, git)
    repository = load_context_records(root, git=gateway)
    head = gateway.head_sha()
    if not isinstance(head, str) or not tasks._COMMIT_SHA_RE.match(head):
        raise ContextRecordError(
            "head_unresolved",
            f"git answered {head!r} for HEAD, which is not a full commit sha; nothing is "
            "stamped from an answer that cannot be re-resolved",
        )
    head_subject = f"git answered HEAD = '{head}'"
    head_tree = _verify_commit(gateway, head, head_subject)

    pending = [record for record in repository.records if not record.stamped]
    already = tuple(record.id for record in repository.records if record.stamped)
    if pending:
        tree_paths = commit_tree_paths(gateway, head_tree, head_subject)
        for record in pending:
            gone = missing_paths_in_tree(record, tree_paths)
            if gone:
                raise ContextRecordError(
                    "unverifiable_record",
                    f"{_label(record.path, record.id)} names {', '.join(gone)}, which "
                    f"commit {head} does not contain — being in the checkout is not the "
                    "claim a stamp makes, so nothing here is stamped",
                )

    rewritten: list[tuple[ContextRecord, Path, str]] = []
    for record in pending:
        file = root / record.path
        lines = _read(file, record.path).split("\n")
        current = lines[record.commit_line] if record.commit_line < len(lines) else ""
        if current != f"last_verified_commit: {UNSTAMPED}":
            raise ContextRecordError(
                "record_changed_under_stamp",
                f"{_label(record.path, record.id)} line {record.commit_line + 1} reads "
                f"{current!r}, not the sentinel this run parsed; the file changed while it "
                "was being read and nothing at all is written",
            )
        lines[record.commit_line] = f"last_verified_commit: {head}"
        rewritten.append((record, file, "\n".join(lines)))

    done: list[str] = []
    for record, file, text in rewritten:
        try:
            _write_atomically(file, text)
        except OSError as exc:
            raise ContextRecordError(
                "unwritable_record",
                f"{_label(record.path, record.id)} could not be written ({exc}). "
                f"{len(done)} record(s) were stamped to {head} before it"
                + (f" ({', '.join(done)})" if done else "")
                + ", every other pending record still reads the sentinel, and each file this "
                "run did write is a whole record — the set is not written atomically, so "
                "re-running stamps the remainder",
            ) from exc
        done.append(record.id)

    stamped = tuple(record.id for record in pending)
    if stamped:
        written = load_context_records(root, git=gateway).by_id()
        for record_id in stamped:
            after = written.get(record_id)
            if after is None:
                raise ContextRecordError(
                    "stamp_vanished",
                    f"context record '{record_id}' was stamped to {head} and is no longer in "
                    f"{CONTEXT_DIR} when the tree is read back; the file it was written to is "
                    "gone, so nothing here can say what that stamp says",
                )
            if after.last_verified_commit != head:
                raise ContextRecordError(
                    "stamp_not_written",
                    f"{_label(after.path, record_id)} still reads "
                    f"{after.last_verified_commit!r} after being stamped to {head}",
                )
    return Stamp(head_sha=head, stamped=stamped, already=already)


USAGE = "usage: python3 -m autoloop.context_records [check|stamp] [repository root]"


def main(argv: list[str] | None = None) -> int:
    """`check` (the default) validates the tree — including putting every
    stamped record's commit to git, since the load is what does that — and
    reports broken pointers; `stamp` does the same and then writes HEAD into
    every UNSTAMPED record. Exit 0 is a pass, 1 a refusal with the reason on
    stderr, 2 a usage error.

    `check` also NAMES every file the load stepped over. Those are non-Markdown
    dotfiles, the one thing under `CONTEXT_DIR` no record contract applies to —
    a record is a Markdown file the index lists, and `.hidden.md` is one — and
    leaving them unmentioned at the operator surface would make a file sitting
    in this tree that nothing validated invisible exactly where a human goes
    looking. It is a report, not a refusal: `.DS_Store` is not a broken
    record."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "check"
    if command not in ("check", "stamp") or len(args) > 2:
        print(USAGE, file=sys.stderr)
        return 2
    root = Path(args[1]) if len(args) > 1 else Path.cwd()
    try:
        if command == "check":
            repository = load_context_records(root)
            for rel in repository.ignored:
                print(f"ignored (not a record): {rel}", file=sys.stderr)
            broken = unverifiable_records(repository)
            for record_id, gone in broken:
                print(f"unverifiable: {record_id} names {', '.join(gone)}", file=sys.stderr)
            if broken:
                return 1
            unstamped = [r.id for r in repository.records if not r.stamped]
            print(
                f"{len(repository.records)} record(s) valid, {len(unstamped)} unstamped"
                + (f": {', '.join(unstamped)}" if unstamped else "")
            )
            return 0
        stamp = stamp_records(root)
        print(
            f"HEAD {stamp.head_sha}: stamped {len(stamp.stamped)}, "
            f"already stamped {len(stamp.already)}"
            + (f" — {', '.join(stamp.stamped)}" if stamp.stamped else "")
        )
        return 0
    except (ContextRecordError, GitError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
