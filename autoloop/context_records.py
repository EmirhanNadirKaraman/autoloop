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
import os
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


def record_to_mapping(record: ContextRecord) -> dict:
    """One record as the JSON object `record_from_mapping` reads back.

    EVERY field is written, including the empty ones, and that is the whole
    design decision here. An omitted key and a key holding the empty value load
    identically today, so dropping the empties would be smaller — and it would
    make a record file stop SAYING that it names no successor, has no invariant
    and asserts about no paths. These files are read by people as well as by
    `load_records`, and the difference between "this record has no successor"
    and "somebody's writer forgot that field" is exactly the difference this
    package spends its docstrings on elsewhere.

    Tuples become lists because that is what JSON has; `record_from_mapping`
    accepts either, so `record_from_mapping(record_to_mapping(r)) == r` for
    every record that parsed in the first place.
    """
    return {
        "id": record.id,
        "kind": record.kind,
        "title": record.title,
        "invariant": record.invariant,
        "source_paths": list(record.source_paths),
        "related_ids": list(record.related_ids),
        "last_verified_commit": record.last_verified_commit,
        "superseded_by": record.superseded_by,
    }


def superseded_record(record: ContextRecord, successor_id: str) -> ContextRecord:
    """`record` with `superseded_by` set — SUPERSEDE, NEVER REWRITE.

    Returns a NEW record and leaves the old value untouched (the dataclass is
    frozen), because the whole point of a supersession is that the old claim
    stays readable: deleting it deletes the reason it was ever made, which is
    the rule `docs/SECURITY.md` keeps for a resolved finding and `CLAUDE.md`
    keeps for a `COMMON_ERRORS` entry. The caller writes this back to the SAME
    file the old record came from and writes the successor to its own file; two
    records, both present, one of them retired.

    Refuses a successor that cannot do the job rather than writing a
    supersession nobody can follow:

      * an empty or padded id — `_require_clean_string` compares ids verbatim,
        so a padded successor is one no record will ever match;
      * the record's OWN id — `context_resolver._supersession_finding` reports
        that as a dangling supersession, and the record would be retired into
        itself with nothing to read instead.

    Whether the successor EXISTS is deliberately not asked here: this module
    holds no index (`context_index` does), and `ContextRecord.superseded_by`
    states that a dangling successor must not turn a retired record back into an
    active one — so an unresolvable one is a finding rather than a refusal, and
    only a caller holding an index can raise it.

    NOT called by the closeout, and that is the point rather than an omission:
    `context_packet.classify_closeout` never supersedes anything, because the
    successor is a claim the loop cannot author. This is the operation the
    follow-up task it files exists to perform.
    """
    if not isinstance(successor_id, str) or not successor_id or successor_id != successor_id.strip():
        raise ContextRecordError(
            f"a supersession of {record.id!r} needs a non-empty, unpadded "
            f"successor id, got {successor_id!r}"
        )
    if successor_id == record.id:
        raise ContextRecordError(
            f"record {record.id!r} cannot supersede itself — that is reported as "
            "a dangling supersession and leaves nothing to read instead"
        )
    return dataclasses.replace(record, superseded_by=successor_id)


class ContextRecordStore:
    """A directory of record files, and WHAT THOSE FILES ARE CALLED IN THE
    REPOSITORY (`repo_prefix`).

    **Two caller-named facts, and neither of them is decided here.** ctx-03 put
    the record SHAPE in this module and deliberately left the location to a
    later round; nothing has named a directory since, and this class does not
    name one either — it takes both the directory it reads and the repository
    path those files have. That second argument is what makes the SCOPE question
    askable at all: a context write is graded like every other write, against
    `tasks.unauthorized_paths` over a repository-relative path, so a store that
    could not say what its files are called in the repository can never be
    written to in scope. Which is the fail-closed direction, and the answer an
    unwired loop gets.

    `repo_prefix=""` therefore means "these files have no repository path I can
    state", and `repo_path_for` answers `""` for every record — so every record
    in such a store is out of scope for every task, and the closeout files its
    follow-up instead of writing anything.

    **`directory` and `repo_prefix` are two facts, not one, and the directory
    may not be inside the observed checkout.** That is port-01's rule reaching a
    new writer, and `orchestrator._store_is_inside_the_observed_checkout` is
    where it is enforced: a record written into the observed tree is an
    uncommitted file this loop cannot commit, and the next write-capable
    dispatch refuses to start against the dirty tree it leaves
    (`primary_checkout_dirty`, loop-fatal). So the files live outside it and the
    prefix says what they are CALLED in it, which is all the scope check needs.

    Deliberately NOT a loader: `load_records` above is the one reader, and
    `context_index.load_index` is the one place the loop builds an index out of
    it. A second read path here would be a second answer to "what is in this
    directory".
    """

    def __init__(self, directory, repo_prefix: str = ""):
        self.directory = Path(directory)
        self.repo_prefix = _clean_repo_prefix(repo_prefix)

    @staticmethod
    def filename_for(record_id: str) -> str:
        """The file a NEW record with this id belongs in, or `""` when the id
        cannot be spelled as a plain file name.

        Only for records this store is about to CREATE. An existing record is
        rewritten to the file it was loaded from (`LoadedRecord.source`), never
        to this — a record loaded from `feature-one.json` and written back to
        `<id>.json` would leave TWO files declaring one id, which
        `context_index` refuses to index under either name. That is the quiet
        way an update deletes a record.
        """
        if not isinstance(record_id, str) or not _is_plain_name(record_id):
            return ""
        return f"{record_id}{_SUFFIX}"

    def path_for(self, filename: str) -> Path | None:
        """Where `filename` lives in this store, or `None` when the name is not
        one plain file name inside it. `None` rather than a path outside the
        directory: the names reaching here come from record ids, and an id is
        any unpadded string (`_require_clean_string`), including one with a '/'
        in it."""
        if not isinstance(filename, str) or not _is_plain_name(filename):
            return None
        if not filename.endswith(_SUFFIX):
            return None
        return self.directory / filename

    def repo_path_for(self, filename: str) -> str:
        """What `filename` is called IN THE REPOSITORY, or `""` when this store
        cannot say. The value handed to `tasks.unauthorized_paths`, so `""` is
        read by every caller as "not inside any scope"."""
        if not self.repo_prefix or self.path_for(filename) is None:
            return ""
        return f"{self.repo_prefix}/{filename}"

    def write(self, record: ContextRecord, filename: str) -> Path | None:
        """Write `record` to `filename` in this store. Returns the path, or
        `None` when nothing was written.

        `None` rather than an exception, for `ContextPacketStore.save`'s reason:
        the CALLER decides what a failed write costs, and the caller here is a
        closeout running after a push has already landed, where a raise would
        turn a bookkeeping problem into a park for work that is already durable.
        Every refusal is a `None` the caller reports:

        * a file name this store will not address (see `path_for`);
        * a record that does not READ BACK as itself. The mapping is re-parsed
          through `record_from_mapping` and compared before anything touches the
          disk, so a record the loader would refuse — or would load as something
          else — never replaces one that loads today. An update that silently
          turned a record into a `RecordProblem` would delete the claim while
          reporting success, which is the failure this whole module is written
          against;
        * an `OSError`.

        Atomic (temp file + `os.replace`), like every other writer here, so a
        concurrent `load_records` sees either the old file or the new one.
        Unlike `ContextPacketStore.save` a failure does NOT remove the file it
        failed to replace: that store's file is a per-round artifact whose
        digest has already moved on, and this one holds the only copy of a claim
        nobody else can reconstruct.
        """
        path = self.path_for(filename)
        if path is None:
            return None
        data = record_to_mapping(record)
        try:
            if record_from_mapping(json.loads(json.dumps(data))) != record:
                return None
        except (ContextRecordError, ValueError, TypeError):
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except OSError:
            return None
        return path


def _is_plain_name(name: str) -> bool:
    """One file name, addressing nothing but a file in the directory it is
    given: no separator, no '.'/'..', no leading dot, and nothing empty."""
    if not name or name.startswith(".") or name != name.strip():
        return False
    return not any(sep in name for sep in ("/", "\\", "\0"))


def _clean_repo_prefix(prefix) -> str:
    """`prefix` as a repository-relative directory path, or `""`.

    The same shape `_check_source_path` demands of a record's own paths, and for
    the same reason: this string is compared against `Task.approved_paths` by
    `tasks.unauthorized_paths`, which does prefix matching on segment
    boundaries. An absolute or `..`-bearing prefix would match nothing there
    while looking like a real location, so it is refused into `""` — "this store
    has no repository path" — which every caller already reads as out of scope.
    """
    if not isinstance(prefix, str):
        return ""
    cleaned = prefix.strip().rstrip("/")
    if not cleaned or cleaned.startswith("/") or "\\" in cleaned:
        return ""
    if any(segment in ("", ".", "..") for segment in cleaned.split("/")):
        return ""
    return cleaned


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
