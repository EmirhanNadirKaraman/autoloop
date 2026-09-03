"""The record a context selection is made out of, and how one is read.

ONE RECORD PER FILE, one JSON object per file, in a directory the CALLER
names. Nothing here decides where those files live: `context_resolver` is pure
given its inputs the way `context.build_context` is, and wiring a location into
the loop is ctx-04's. What this module fixes is the SHAPE, so that the index
and the resolver above it can be reasoned about without re-deciding what a
record is at every call site.

A record is a claim about SOURCE PATHS at a COMMIT. That pairing is the whole
design:

* `source_paths` — the repository-relative files the record asserts about.
* `last_verified_commit` — the commit those assertions were checked against.

Staleness is then a question about trees rather than about history (see
`context_resolver`), and every other field exists to make a selection
reviewable: `related_ids` is the only edge the resolver may follow,
`superseded_by` is the only way a record stops being active, and `invariant` is
what two records can disagree about.

**Reading is TOLERANT but never SILENT.** A file that will not parse, names a
kind that does not exist or carries a key nobody defined does not vanish and
does not stop the load: it becomes a `RecordProblem`, which the index carries
and the resolver reports. Dropping a malformed record quietly is the exact
fail-open this whole roadmap item exists to prevent — a context selection that
is missing the one record that contradicted it, and says nothing.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: The four kinds a record may have, and the only four. Named by the task that
#: introduced this file: the resolver expands to "explicitly related feature,
#: incident, decision and lesson records" and to nothing else, so a fifth kind
#: is a change to what the resolver selects and belongs in a reviewed commit
#: rather than in a data file.
#:
#: SORTED, and used as such: `kind` is the first component of the total order
#: every result is returned in, so this tuple's own spelling never matters —
#: the comparison is on the string.
RECORD_KINDS: tuple[str, ...] = ("decision", "feature", "incident", "lesson")

#: The extension `load_records` reads. One record per file, so a directory can
#: hold notes, a README or an index without any of them being read as a record.
_SUFFIX = ".json"


class ContextRecordError(ValueError):
    """A record file that cannot be turned into a `ContextRecord`.

    Deliberately NOT in `autoloop/errors.py`: nothing outside this module and
    its two readers raises or catches it, and the loader below turns it into a
    `RecordProblem` rather than letting it escape. `ValueError` is the base so
    a caller that does let one through gets something an ordinary `except`
    already understands.
    """


@dataclass(frozen=True)
class ContextRecord:
    """One context record. Immutable, comparable, and ordered by `order_key`.

    Every field except `id` and `kind` has a default, and each default is the
    honest empty reading of its own question — no related records, no source
    paths, not superseded, asserting no invariant. `last_verified_commit` is
    the one where "empty" is NOT a benign default: a record with no commit has
    never been verified against anything, so the resolver reports its staleness
    as UNKNOWN rather than as fresh. It is defaulted anyway, because refusing
    the record outright would delete the claim instead of flagging it.
    """

    #: Unique across the index. Compared verbatim, so padding is refused at
    #: parse time rather than silently stripped — an id with a trailing space
    #: is one no seed list a human typed will ever match.
    id: str
    #: One of `RECORD_KINDS`.
    kind: str
    #: One line, for the reason a reviewer reads. Never rendered raw: the
    #: resolver collapses whitespace, because a title with a newline in it
    #: would break a line-oriented block.
    title: str = ""
    #: What this record ASSERTS about its source paths, in one line. Empty
    #: means it asserts nothing checkable, and a record asserting nothing can
    #: never contradict another — see `context_resolver._contradictions`.
    invariant: str = ""
    #: Repository-relative file paths, verbatim as git spells them. Checked for
    #: being relative and traversal-free at parse time, because they are
    #: compared against `git ls-tree` output and an absolute or `..`-bearing
    #: path can never match one.
    source_paths: tuple[str, ...] = ()
    #: The ONLY edge the resolver follows. Directed as written: naming B here
    #: pulls B in when this record is selected, and does NOT pull this record
    #: in when B is.
    related_ids: tuple[str, ...] = ()
    #: The commit `source_paths` were last checked against.
    last_verified_commit: str = ""
    #: The id of the record that replaces this one. NON-EMPTY IS THE WHOLE
    #: ASSERTION: a record is superseded because it says so, whether or not the
    #: successor can be resolved. Gating it on resolving the successor would
    #: mean a dangling id turns a retired record back into an active one, which
    #: is exactly the "never returned as active" guarantee inverted.
    superseded_by: str = ""

    @property
    def is_superseded(self) -> bool:
        return bool(self.superseded_by)

    @property
    def order_key(self) -> tuple[str, str]:
        """KIND THEN ID — the total order every result is returned in.

        Total because ids are unique in an index (duplicates are excluded from
        it, see `context_index`), so no two records can compare equal. That is
        the same reasoning `Task.priority`'s id tiebreak carries for
        `next_ready`: an order that can tie is an order that falls back to dict
        or filesystem iteration, which is not an order at all.
        """
        return (self.kind, self.id)


@dataclass(frozen=True)
class RecordProblem:
    """A record file that could not be read, named rather than dropped.

    `source` is the FILE NAME, not the full path: the problems are rendered
    into the resolver's block, and an absolute path would make that block
    differ between two checkouts of the same repository — i.e. would make the
    byte-identical-output claim false for a reason that has nothing to do with
    the records. The one exception is a problem with the DIRECTORY itself,
    which has no file name to give and carries the path as written.
    """

    source: str
    message: str

    @property
    def order_key(self) -> tuple[str, str]:
        return (self.source, self.message)


@dataclass(frozen=True)
class LoadedRecord:
    """A record plus WHERE it came from, which duplicate reporting needs.

    Two files declaring one id must both be nameable — "one of your records is
    a duplicate" is not a report anybody can act on — so the source travels
    with the record until the index has grouped them.
    """

    record: ContextRecord
    source: str


def _require_clean_string(data: Mapping, key: str, *, required: bool) -> str:
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ContextRecordError(f"{key} must be a string, got {value!r}")
    if value != value.strip():
        raise ContextRecordError(
            f"{key} must not be padded with whitespace, got {value!r} — it is "
            "compared verbatim against ids written by hand"
        )
    if required and not value:
        raise ContextRecordError(f"{key} is required and must not be empty")
    return value


def _require_string_tuple(data: Mapping, key: str) -> tuple[str, ...]:
    value = data.get(key, ())
    if value is None:
        value = ()
    # `str` first: a bare string is a Sequence of characters, so an unguarded
    # check would read `"a.py"` as five one-character paths.
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContextRecordError(
            f"{key} must be a list of strings, got {value!r} — write a single "
            "entry as a one-element list, not as a bare string"
        )
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry or entry != entry.strip():
            raise ContextRecordError(
                f"{key} entries must be non-empty unpadded strings, got {entry!r}"
            )
        out.append(entry)
    return tuple(out)


def _check_source_path(path: str) -> None:
    """Refuse a source path git could never name.

    These are compared against `ls-tree` and `diff-tree` output, which is
    always repository-relative with forward slashes and no `.`/`..` segments.
    A path outside that shape matches nothing, so it would be reported MISSING
    forever while looking like a real assertion about a real file — the loud
    failure is here, at parse time, naming the record.
    """
    if path.startswith("/"):
        raise ContextRecordError(
            f"source_paths entries must be repository-relative, got {path!r}"
        )
    if path.startswith("./") or "\\" in path:
        raise ContextRecordError(
            f"source_paths entries must be spelled as git spells them, got {path!r}"
        )
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ContextRecordError(
            "source_paths entries must have no empty, '.' or '..' segments, "
            f"got {path!r}"
        )


def record_from_mapping(data: Mapping) -> ContextRecord:
    """One parsed JSON object as a `ContextRecord`, or `ContextRecordError`.

    STRICT about unknown keys, in `config.load_config`'s style and for its
    reason: a typo'd `source_path` (singular) would otherwise load as a record
    asserting nothing about no files, which is a claim that can never be found
    stale, missing or contradictory. A silently ignored key is a guard that
    switched itself off.
    """
    if not isinstance(data, Mapping):
        raise ContextRecordError(f"a record must be a JSON object, got {data!r}")
    allowed = {f.name for f in dataclasses.fields(ContextRecord)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ContextRecordError(
            f"unknown keys: {unknown} — known keys are {sorted(allowed)}"
        )
    record_id = _require_clean_string(data, "id", required=True)
    kind = _require_clean_string(data, "kind", required=True)
    if kind not in RECORD_KINDS:
        raise ContextRecordError(
            f"kind must be one of {list(RECORD_KINDS)}, got {kind!r}"
        )
    source_paths = _require_string_tuple(data, "source_paths")
    for path in source_paths:
        _check_source_path(path)
    return ContextRecord(
        id=record_id,
        kind=kind,
        title=_require_clean_string(data, "title", required=False),
        invariant=_require_clean_string(data, "invariant", required=False),
        source_paths=source_paths,
        related_ids=_require_string_tuple(data, "related_ids"),
        last_verified_commit=_require_clean_string(
            data, "last_verified_commit", required=False
        ),
        superseded_by=_require_clean_string(data, "superseded_by", required=False),
    )


def load_records(directory) -> tuple[tuple[LoadedRecord, ...], tuple[RecordProblem, ...]]:
    """Every `*.json` in `directory`, read once, in FILE NAME order.

    Returns `(records, problems)` and never raises for a bad file: one
    unreadable record must not take the other forty with it, and it must not
    disappear either. Sorted by name so two loads of one directory produce the
    same order whatever `os.scandir` felt like doing — the resolver sorts its
    own output as well, but a loader whose order depends on the filesystem
    makes every claim above it harder to believe than it needs to be.

    A directory that does not exist, or is not a directory, is ONE problem
    rather than an exception: the caller is handed an empty index that reports
    why it is empty, which is strictly louder than an empty index that does
    not. Every seed then resolves to `unknown_record` on top of it.
    """
    directory = Path(directory)
    problems: list[RecordProblem] = []
    records: list[LoadedRecord] = []
    if not directory.is_dir():
        return (), (
            RecordProblem(
                source=str(directory),
                message=(
                    "context record directory does not exist or is not a "
                    "directory, so no record could be loaded at all"
                ),
            ),
        )
    for path in sorted(directory.glob(f"*{_SUFFIX}")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(RecordProblem(path.name, f"unreadable: {exc}"))
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(RecordProblem(path.name, f"not valid JSON: {exc}"))
            continue
        try:
            records.append(LoadedRecord(record_from_mapping(data), path.name))
        except ContextRecordError as exc:
            problems.append(RecordProblem(path.name, str(exc)))
    return tuple(records), tuple(problems)
