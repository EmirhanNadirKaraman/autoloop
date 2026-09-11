"""The record a context selection is made out of, and how one is read.

ONE RECORD PER FILE, one JSON object per file, in a directory the CALLER
names. What this module fixes is the SHAPE, so that the index and the resolver
above it can be reasoned about without re-deciding what a record is at every
call site — `context_resolver` stays pure given its inputs the way
`context.build_context` is.

WHERE THEY LIVE, since ctx-16 answered it: IN THE TARGET REPOSITORY, at
`[context] records_dir` (`docs/context` by default), read through
`repository_record_store` below and never written by this loop. Knowledge about
a project belongs to that project's history, where it is versioned, reviewed and
travels with the commit it describes. The loop-private `ContextRecordStore` is
still the general case and still writes; the repository-backed subclass is the
one production builds, and it refuses every write for the reason its own
docstring gives.

AND WHEN THEY ARE READ, which is the other half of "travel with the commit": a
repository-backed store is read OUT OF GIT OBJECTS AT A NAMED REVISION
(`load_records_at`, through `RepositoryContextRecordStore.load`), never off the
observed checkout's working tree. The packet a round is given names
`task_base_sha` as the commit it was rendered from, and the resolver grades
staleness against that same commit — so the record BYTES have to come from it
too. A working tree is whatever the observed branch is at right now, which is a
later commit than the base whenever the branch advanced after the task was cut
(a resumed round on a reused worker keeps its stale base by design, wrk-01), and
a packet that quoted those later bytes under the base's sha would be provenance
that lies. Git objects at a sha are immutable, so a read at `task_base_sha` is
the same bytes on the dispatch that renders the packet and on the closeout that
confirms it, however far the checkout has moved in between.

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
is missing the one record that contradicted it, and says nothing. Both loaders
below hand every file's bytes to ONE parser (`_parse_record_bytes`), so a
malformed record becomes the same problem whether it was read off a disk or out
of a blob.

**AND A RECORD IS VALIDATED, NOT MERELY PARSED (ctx-14, porting ctx-02's checks
onto this module).** A record that parses is still prose nobody can check until
its citations are RESOLVED: `last_verified_commit` must be an object the
repository holds, `superseded_by` must name a record that is actually present,
and every `related_ids` entry must name one too. Those are `verify_records`
below, run by both loaders after parsing — so they run on the store, whatever a
caller built on top of it — and every failure is one more `RecordProblem` for a
record that is then NOT returned. The shape half (a kind's required fields, the
title cap, a commit spelled as one commit) is refused at parse time in
`record_from_mapping`, which is also what `ContextRecordStore.write` re-reads a
record through before it touches the disk.

EVERY CHECK FAILS CLOSED. A check that cannot run — no gateway to resolve a
commit in, a gateway that raises, a gateway without the probe — reports the
record as unverifiable rather than passing it, because "the check could not run"
and "the check ran and passed" are the two answers this file exists to keep
apart. The starvation cases and what each does are listed on `verify_records`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .errors import GitError

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

#: What a KIND has to carry to be checkable — ctx-02's required sections,
#: expressed against this module's fields rather than Markdown headings. Every
#: kind needs a `title` (the one line a reviewer reads; the packet renders
#: `(no title)` for a record without one, which is a record nobody can review).
#: Beyond that, each kind requires the field its own claim is MADE OF:
#:
#: * `feature` — a documented invariant over files (`context_packet.
#:   VERIFIABLE_KINDS`: "records whose claim is about FILES"), so `invariant`
#:   and `source_paths`;
#: * `incident` — what happened to which files, so `source_paths`; an incident
#:   need assert no invariant (`context_resolver._report_contradictions` skips a
#:   record that asserts none, and ctx-03's own tests hold an incident that way);
#: * `decision` — a checkable assertion, `invariant`, which is what a successor
#:   supersedes and what two records can disagree about;
#: * `lesson` — the title alone. `context_packet._lesson_for` writes lessons with
#:   an empty invariant and no source paths ON PURPOSE (a lesson is not a claim
#:   about files and must never contradict a record a person wrote), so a lesson
#:   requiring either would refuse the only lesson this loop ever authors.
#:
#: A kind absent from this table is refused by `record_from_mapping` rather than
#: waved through with no rule: the table and `RECORD_KINDS` are pinned to agree
#: by `test_context_record_checks.py`, and a mismatch fails closed in between.
REQUIRED_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "decision": ("title", "invariant"),
        "feature": ("title", "invariant", "source_paths"),
        "incident": ("title", "source_paths"),
        "lesson": ("title",),
    }
)

#: The longest `title` a record may carry — ctx-02's `MAX_SUMMARY_CHARS`, applied
#: to the field that IS this module's summary. A title is rendered on one line of
#: every packet and every resolution block; one that runs to a paragraph is a
#: record whose claim has moved out of the fields that are checked and into prose
#: that is not. Exactly this many characters passes; one more is refused.
MAX_SUMMARY_CHARS = 200

#: The states a record can be in, and the only two — ctx-02's `STATUSES`, cut
#: to what this module's fields can actually distinguish. A record is
#: `superseded` because `superseded_by` is non-empty and `active` otherwise
#: (`ContextRecord.status`); that is DERIVED and never asserted, so a file
#: carrying a `status` key is refused as an unknown key rather than read as a
#: claim. ctx-02's `resolved` and `retired` are deliberately not here: nothing in
#: this loop decides either (the closeout files a follow-up instead), and a
#: vocabulary entry nothing reads is a claim nobody checks.
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_SUPERSEDED)

#: How `last_verified_commit` must be spelled when it is given at all: one full
#: lowercase object id — 40 hex for SHA-1, 64 for a SHA-256 repository, the
#: same pair `tasks._COMMIT_SHA_RE` accepts — the spelling git prints and the
#: closeout writes (`published_sha`). `HEAD`, a branch name or an abbreviation
#: would each RESOLVE — `cat-file -e` accepts any revision expression — and
#: each names a different commit at a different time or in a different clone,
#: so "verified against X" would be a claim that moves. Lowercase only because
#: the closeout compares this field VERBATIM against the sha it published, and
#: an uppercase spelling would be rewritten as if it were a different commit.
#: Used with `fullmatch`: a `$` anchor would forgive a trailing newline.
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

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
    #: The commit `source_paths` were last checked against. Spelled as one full
    #: object id (`_FULL_SHA`) or empty, and RESOLVED by the loader through the
    #: worker's gateway (`verify_records`): a record naming a commit the
    #: repository does not hold, or an object that is not a commit, is not
    #: loaded, because that citation is exactly the prose nobody can check.
    last_verified_commit: str = ""
    #: The id of the record that replaces this one. NON-EMPTY IS THE WHOLE
    #: ASSERTION: a record is superseded because it says so, whether or not the
    #: successor can be resolved. Gating it on resolving the successor would
    #: mean a dangling id turns a retired record back into an active one, which
    #: is exactly the "never returned as active" guarantee inverted. The loader
    #: goes one step further and REFUSES a record whose successor no record file
    #: declares (`verify_records`) — refused is still not active, and the
    #: resolver's `dangling_supersession` stays as the answer for an index built
    #: from records that never went through a loader.
    superseded_by: str = ""

    @property
    def is_superseded(self) -> bool:
        return bool(self.superseded_by)

    @property
    def status(self) -> str:
        """One of `STATUSES`, derived from `superseded_by` and nothing else."""
        return STATUS_SUPERSEDED if self.is_superseded else STATUS_ACTIVE

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

    STRICT about SHAPE too, since ctx-14: the fields `REQUIRED_FIELDS` names for
    the record's kind must be non-empty, `title` is capped at
    `MAX_SUMMARY_CHARS`, and a non-empty `last_verified_commit` must be one full
    lowercase object id (`_FULL_SHA`). Each is refused here, naming the record,
    rather than loaded as a record that looks complete and is not. Whether that
    commit EXISTS is not a shape question and is asked by `verify_records`,
    which has a gateway to ask it of.
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
    required = REQUIRED_FIELDS.get(kind)
    if required is None:
        # A kind with no required-field rule is refused, not waved through: the
        # table and `RECORD_KINDS` are meant to agree, and the gap between them
        # must not be a record that loads with nothing checked.
        raise ContextRecordError(
            f"kind {kind!r} has no required-field rule in REQUIRED_FIELDS, so "
            "no record of that kind can be validated"
        )
    source_paths = _require_string_tuple(data, "source_paths")
    for path in source_paths:
        _check_source_path(path)
    title = _require_clean_string(data, "title", required=False)
    if len(title) > MAX_SUMMARY_CHARS:
        raise ContextRecordError(
            f"title is {len(title)} characters, over the {MAX_SUMMARY_CHARS} a "
            "one-line summary may have — a claim that needs a paragraph belongs "
            "in a field that is checked, not in the title"
        )
    commit = _require_clean_string(data, "last_verified_commit", required=False)
    if commit and not _FULL_SHA.fullmatch(commit):
        raise ContextRecordError(
            f"last_verified_commit must be one full lowercase object id (40 hex, "
            f"or 64 in a SHA-256 repository) or empty, got {commit!r} — a ref, an "
            "abbreviation or an uppercase spelling names a different commit at a "
            "different time or in a different clone"
        )
    record = ContextRecord(
        id=record_id,
        kind=kind,
        title=title,
        invariant=_require_clean_string(data, "invariant", required=False),
        source_paths=source_paths,
        related_ids=_require_string_tuple(data, "related_ids"),
        last_verified_commit=commit,
        superseded_by=_require_clean_string(data, "superseded_by", required=False),
    )
    missing = [field for field in required if not getattr(record, field)]
    if missing:
        raise ContextRecordError(
            f"a {kind} record must carry {list(required)}, and {record_id!r} "
            f"leaves {missing} empty — without it the record makes no claim "
            "this kind can be checked on"
        )
    return record


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

    Whether the successor EXISTS is deliberately not asked here: this function
    holds one record, not the directory, and `ContextRecord.superseded_by`
    states that a dangling successor must not turn a retired record back into an
    active one. It is asked where the whole directory is in hand — the loader's
    `verify_records`, which refuses the superseded record on the next load if
    the caller never wrote the successor's file — and by the resolver, as a
    finding, for an index built without a loader.

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

    **`directory` and `repo_prefix` are two facts, not one, and a WRITING
    store's directory may not be inside the observed checkout.** That is
    port-01's rule reaching a new writer, and
    `orchestrator._store_would_write_inside_the_observed_checkout` is where it is
    enforced: a record written into the observed tree is an uncommitted file this
    loop cannot commit, and the next write-capable dispatch refuses to start
    against the dirty tree it leaves (`primary_checkout_dirty`, loop-fatal). So a
    store of THIS class lives outside the tree and the prefix says what its files
    are CALLED in it, which is all the scope check needs.

    The qualification is `writes_directly` below, and it is the difference
    `RepositoryContextRecordStore` exists to make: a store that cannot write
    cannot leave an uncommitted file, so it may read a directory inside the
    observed checkout. Reading never dirties a tree, and the detector still
    watches that tree completely — nothing is excluded from it, which is the
    property port-01 moved `state_dir` out to get.

    `load` is the store's ONE answer to "what records do you hold", and it is a
    dispatcher rather than a third reader: this class answers with
    `load_records` over its directory, the repository-backed subclass with
    `load_records_at` over a revision, and both readers hand every file's bytes
    to the same parser. The loop asks a store and never picks a reader itself —
    `orchestrator._context_record_index` and `context_packet.plan_round_closeout`
    both call `store.load(worktree_git, task_base_sha)` — because two call sites
    each choosing a reader is how the packet and the closeout come to read two
    different trees.
    """

    #: Does `write` on this store put bytes on disk? TRUE here — a store of this
    #: class is the loop's own private directory and writing to it is the whole
    #: point — and FALSE on `RepositoryContextRecordStore`, whose records are
    #: repository files a reviewed round authors. Every caller that asks reads it
    #: through ONE accessor (`orchestrator._store_writes_directly`), which
    #: defaults an object that does not answer to WRITABLE: an unknown store is
    #: treated as one that would dirty a tree, which is the refusing direction.
    writes_directly = True

    def __init__(self, directory, repo_prefix: str = ""):
        self.directory = Path(directory)
        self.repo_prefix = clean_repo_prefix(repo_prefix)

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

    def load(
        self, git=None, rev: str = ""
    ) -> tuple[tuple[LoadedRecord, ...], tuple[RecordProblem, ...]]:
        """Every record this store holds, as `load_records` answers it — off the
        DISK, because that is the only place a loop-private store's files exist.

        `git` and `rev` are accepted so that every caller asks every store the
        same question (`store.load(worktree_git, task_base_sha)`). `rev` is
        deliberately IGNORED here: this store is not versioned, so there is no
        revision of it to read. `git` is NOT ignored — it is the gateway the
        records' own commit citations are resolved through (`verify_records`),
        and handed `None` the loader reports every record naming a commit as
        unverifiable rather than accepting it. The repository-backed subclass is
        the one for which the two arguments also decide which BYTES come back,
        and its override says so.
        """
        return load_records(self.directory, git)


class RepositoryContextRecordStore(ContextRecordStore):
    """Records that live IN THE TARGET REPOSITORY — versioned, reviewed and
    travelling with the commit they describe — and therefore never written by
    this loop directly (ctx-16).

    **WHY THE REPOSITORY.** ctx-02 designed `docs/context`, ctx-12 says a
    round's context must come from the target CHECKOUT AND COMMIT, and
    ctx-10/ctx-11 speak of a repository installing and proving a context
    contract. All three mean the same thing: the knowledge is about the project,
    so it belongs to the project's history rather than to this loop's state
    directory. A record beside `state_dir` would be unversioned, unreviewed, and
    would not survive the loop being pointed at a different checkout.

    **WHY IT CANNOT WRITE, and why that is the design rather than a limitation.**
    `directory` here is inside a checkout, so a write would leave a file the
    closeout cannot commit — it runs AFTER the push has landed, so there is no
    commit left to put it in — and the next write-capable dispatch refuses to
    start against the dirty tree (`primary_checkout_dirty`, loop-fatal). The
    answer is not to write more carefully: it is that a record is a claim, and a
    claim this repository keeps goes in through review like every other one. So
    `write` refuses unconditionally and the closeout names the record in the ONE
    narrow follow-up task `context_packet.follow_up_request` files, whose
    `approved_paths` are exactly the record files it would touch. The agent of
    that round writes them, a reviewer reads them, and they are committed.

    The refusal lives HERE, in the class, and not only in the caller that knows
    about `writes_directly`: a guard that is one forgotten branch away from
    dirtying the observed checkout is a guard that switches itself off the first
    time somebody adds a second call site.

    **AND IT IS READ OUT OF GIT, AT A REVISION, NEVER OFF THE WORKING TREE.**
    `directory` is still the directory of the observed checkout — the location
    guard resolves it and the closeout transcript names it — but `load` below
    does not open it. The packet a round is given says it was rendered from
    `task_base_sha`, the resolver grades every record's staleness against that
    commit, and the observed working tree is whatever commit the branch is at
    NOW: later than the base whenever the branch advanced after the task was
    cut, which a resumed round on a reused worker does by design (wrk-01) and
    an operator committing mid-dispatch does by accident. Reading the tree would
    quote those later bytes under the base's sha. So the records come from the
    worker's own object database at the base, the same discipline every other
    line of the packet already follows (`context_packet`'s "the commit is the
    worker's base, not the checkout's head").
    """

    #: See `ContextRecordStore.writes_directly`. FALSE, and `write` below
    #: enforces it independently — the flag tells a caller what will happen, the
    #: method makes it true.
    writes_directly = False

    def load(
        self, git=None, rev: str = ""
    ) -> tuple[tuple[LoadedRecord, ...], tuple[RecordProblem, ...]]:
        """`load_records_at(git, rev, self.repo_prefix)` — the records as they
        stand in the tree of `rev`, read through `git`.

        NO FALLBACK TO `self.directory`, and that absence is the guard. Handed no
        gateway or no revision, this answers an empty load carrying ONE problem
        that says which was missing, rather than quietly reading the working
        tree — because the working tree is the one source this class exists not
        to read, and a caller that forgot the revision would otherwise get the
        exact provenance drift back under a store that claims to have fixed it.

        The same gateway then resolves every record's commit citation
        (`verify_records`, inside `load_records_at`): the worker's object
        database holds the history the records describe, so a commit a record
        cites and the worker cannot find is a citation nobody can check.
        """
        if git is None or not rev:
            missing = "no revision" if git is not None else "no repository"
            return (), (
                RecordProblem(
                    source=self.repo_prefix or str(self.directory),
                    message=(
                        f"context records at {self.repo_prefix!r} could not be "
                        f"read: {missing} was given to read them at, and this "
                        "store never reads the working tree instead"
                    ),
                ),
            )
        return load_records_at(git, rev, self.repo_prefix)

    def write(self, record: ContextRecord, filename: str) -> Path | None:
        """Always `None` — this store never puts bytes in a checkout.

        `None` is the value `ContextRecordStore.write` already answers for every
        refusal, so a caller that does not know about `writes_directly` still
        treats the record as one that was not written and still owes it to the
        follow-up. The difference the caller SHOULD make is the reason it reports
        (a deliberate deferral is not a failed write), and
        `orchestrator._close_out_context` makes it before ever calling this.
        """
        return None


def repository_record_store(checkout_root, records_dir) -> RepositoryContextRecordStore | None:
    """The record store for `records_dir` inside `checkout_root`, or `None`.

    THE ONE place a repository-relative `[context] records_dir` becomes a
    store, so the packet a round is given and the closeout that grades it
    cannot be pointed at two different trees by two callers agreeing. The
    directory it names is the LOCATION — what the location guard resolves and
    the transcript reports — and not what is read: `RepositoryContextRecordStore
    .load` reads that directory out of git at the revision it is handed.

    `None` — read everywhere above as "no record store is wired into this loop"
    and reported as such — for the three inputs that cannot name a location:

    * an empty `records_dir`, which is the supported way to turn the mechanism
      off (`[context] records_dir = ""`);
    * a `records_dir` that is not a repository-relative directory path. Cleaned
      by `clean_repo_prefix`, the same rule a record's own `source_paths` are
      held to, because this string is also what `tasks.unauthorized_paths` is
      handed;
    * a checkout root that is not an ABSOLUTE path. `Path("")` is `Path(".")`,
      so a relative root would silently read records out of whatever directory
      the process happens to be standing in — the failure `cli`'s `context
      explain` already guards the same way.
    """
    prefix = clean_repo_prefix(records_dir)
    if not prefix or checkout_root is None:
        return None
    root = Path(checkout_root)
    if not str(root) or not root.is_absolute():
        return None
    return RepositoryContextRecordStore(root / prefix, prefix)


def _is_plain_name(name: str) -> bool:
    """One file name, addressing nothing but a file in the directory it is
    given: no separator, no '.'/'..', no leading dot, and nothing empty."""
    if not name or name.startswith(".") or name != name.strip():
        return False
    return not any(sep in name for sep in ("/", "\\", "\0"))


def clean_repo_prefix(prefix) -> str:
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


def load_records(
    directory, git=None
) -> tuple[tuple[LoadedRecord, ...], tuple[RecordProblem, ...]]:
    """Every `*.json` in `directory`, read once, in FILE NAME order, and then
    VERIFIED through `git` — the reader for a directory ON DISK, i.e. a
    loop-private `ContextRecordStore`. `load_records_at` below is the same
    reader for a directory IN A COMMIT.

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

    `git` is the gateway the records' commit citations are resolved in
    (`verify_records`); the loop hands the worker's. It DEFAULTS TO `None` so
    the signature every caller had still works, and `None` is not "skip the
    check": a record naming a commit is then reported as unverifiable and is not
    returned, while records naming no commit load as before. A caller reading a
    directory of records that cite commits has to say where those commits live.
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
            raw = path.read_bytes()
        except OSError as exc:
            problems.append(RecordProblem(path.name, f"unreadable: {exc}"))
            continue
        _collect(_parse_record_bytes(path.name, raw), records, problems)
    return _verified(records, problems, git)


def load_records_at(
    git, rev: str, prefix: str
) -> tuple[tuple[LoadedRecord, ...], tuple[RecordProblem, ...]]:
    """Every `*.json` DIRECTLY under `prefix` in the tree of `rev`, read out of
    `git`'s object database, in FILE NAME order — `load_records` for a commit
    instead of a directory, and the reader `RepositoryContextRecordStore.load`
    is made of.

    The same tolerance and the same shape: `(records, problems)`, never an
    exception. `git` is a `GitGateway` (or anything answering `tree_of`,
    `tree_entries` and `blob_bytes`), and every refusal it can make is one
    problem the index carries rather than an empty index that looks like a
    repository with no records:

    * a `rev` that will not resolve, or a tree that will not list — ONE problem
      naming the revision, and no records, because "could not read the records
      at this commit" and "this commit holds no records" are different
      repairs;
    * a `prefix` absent from the tree — ONE problem, the same one a missing
      directory earns on disk, so the packet still reads
      `0 indexed, 0 duplicated id(s), 1 unreadable`;
    * a blob that will not read, is not UTF-8, is not JSON or is not a record —
      a problem naming the FILE, by its bare name, and the other records load.

    `source` is the BARE FILE NAME, exactly as `load_records` reports it and
    for a reason beyond consistency: `ContextRecordStore.repo_path_for` refuses
    any name with a separator in it, so a source spelled as the tree path would
    make every repository record unnameable — out of scope everywhere, and
    stripped from the follow-up's `approved_paths` — without one line saying so.

    DIRECT CHILDREN ONLY, and only entries git types as `blob`. `tree_entries`
    lists recursively, so `prefix/sub/x.json` is in the listing and is skipped
    here, because it is not a record on disk either (`load_records` does not
    recurse) and a loader that found records the other one would not is two
    answers to "what is in this directory". A submodule or a tree under a
    `.json` name is reported by name rather than read as bytes.

    Then VERIFIED through the same `git` (`verify_records`), exactly as
    `load_records` verifies a directory on disk: the records at `rev` cite
    commits, successors and relations, and each citation is resolved before the
    record is returned. A gateway that could list the tree but cannot answer
    `object_exists` reports the citing records rather than passing them.
    """
    given = prefix
    prefix = clean_repo_prefix(prefix)
    if not prefix:
        return (), (
            RecordProblem(
                source=str(given),
                message=(
                    "context record directory has no usable repository-relative "
                    "path, so no record could be loaded at all"
                ),
            ),
        )
    try:
        entries = git.tree_entries(git.tree_of(rev))
    except (GitError, OSError) as exc:
        # `OSError` too: a worker directory that vanished between the check
        # that it exists and this call reaches `subprocess` as one, and a loader
        # that promises never to raise has to keep the promise there as well.
        return (), (
            RecordProblem(
                source=prefix,
                message=(
                    f"the tree of {rev or '(none)'} could not be read, so no "
                    f"record could be loaded at all: {_one_line(exc)}"
                ),
            ),
        )
    head = f"{prefix}/"
    present = False
    candidates: list[tuple[str, str, str]] = []
    for path, (_mode, kind, oid) in entries.items():
        if not path.startswith(head):
            continue
        present = True
        name = path[len(head):]
        if "/" in name or not name.endswith(_SUFFIX):
            continue
        candidates.append((name, kind, oid))
    if not present:
        return (), (
            RecordProblem(
                source=prefix,
                message=(
                    f"context record directory does not exist at {rev}, so no "
                    "record could be loaded at all"
                ),
            ),
        )
    problems: list[RecordProblem] = []
    records: list[LoadedRecord] = []
    for name, kind, oid in sorted(candidates):
        if kind != "blob":
            problems.append(RecordProblem(name, f"not a file in the tree of {rev} ({kind})"))
            continue
        try:
            raw = git.blob_bytes(oid)
        except (GitError, OSError) as exc:
            problems.append(RecordProblem(name, f"unreadable: {_one_line(exc)}"))
            continue
        _collect(_parse_record_bytes(name, raw), records, problems)
    return _verified(records, problems, git)


def _verified(
    records: list, problems: list, git
) -> tuple[tuple[LoadedRecord, ...], tuple[RecordProblem, ...]]:
    """The last step of BOTH loaders: `verify_records` over what parsed, with
    its problems joined to the parse problems and the whole list put in
    `RecordProblem.order_key` order — so a file's problem sorts under its name
    whether the file failed to parse or parsed and failed a check."""
    accepted, refused = verify_records(records, git)
    return accepted, tuple(sorted(list(problems) + list(refused), key=lambda p: p.order_key))


#: The exceptions a gateway can raise while being asked about one object, each
#: of which is "the check could not run" and none of which may escape a loader
#: that promises never to raise. `GitError` is git refusing or dying, `OSError`
#: a worker directory that vanished under `subprocess`, and `AttributeError` a
#: gateway WITHOUT the probe — a test double, or an object that is not a
#: `GitGateway` at all. The third is caught on purpose and named in the
#: problem: a gateway that cannot be asked is a gateway that cannot answer, and
#: the fail-closed direction for it is the same as for one that raises.
_GATEWAY_FAILURES = (GitError, OSError, AttributeError)


def verify_records(
    loaded, git
) -> tuple[tuple[LoadedRecord, ...], tuple[RecordProblem, ...]]:
    """`(accepted, problems)` — the records among `loaded` whose citations all
    RESOLVE, and one problem per citation that did not, naming the file.

    THE CHECKS, each ctx-02's, each expressed over this module's fields:

    * `last_verified_commit`, when non-empty, is an object `git` holds
      (`object_exists`) and one `cat-file commit` reads — a commit, or a tag
      over one (`read_commit`). Asked ONCE per distinct commit
      (`_verify_commits`), so forty records verified at one commit cost one
      pair of probes;
    * `superseded_by`, when non-empty, names a record some file in THIS load
      parsed as, and not the record itself;
    * every `related_ids` entry names a record some file in this load parsed as.

    Citations are checked against the PARSED set, not the accepted one, and
    that is deliberate: a record refused for its own commit does not make every
    record that relates to it unreadable in turn. One bad citation refuses one
    record and is reported once, at its source; the resolver still reports the
    edge INTO a refused record as `unknown_record` when it follows it.

    A record that fails any check is NOT among `accepted`. That is what makes
    the problem honest — the resolver renders every index problem as a record
    that "is in no index and can be selected by nothing", and a problem for a
    record that was indexed anyway would be a sentence the reader cannot act on.

    ONE EXCEPTION, for the reason `context_index` gives: every copy of an id
    that MORE THAN ONE file declares is passed through to `accepted` whatever
    its checks said, with its problems reported as well. Dropping only the copy
    that failed would leave the other as the sole record under that id — a
    duplicate resolved by which file happened to verify, which is exactly the
    "one of them wins" the index refuses to let happen. Passed through, both
    reach `build_index`, which indexes the id under neither and names both
    files; such a copy is in no index either way, so its problem line still
    reads true.

    WHAT STARVES EACH CHECK, AND WHAT IT DOES — none of these passes:

    * `git is None` — every record naming a commit is reported as unverifiable
      ("no repository was given") and refused; records naming no commit are
      unaffected, because they cite nothing;
    * `git.object_exists` raises (`_GATEWAY_FAILURES`) — the same, with the
      error on the problem, per distinct commit; a gateway that answers for one
      commit and dies on another refuses only the records at the second;
    * `git.object_exists` answers `False` — "does not exist", refused;
    * the object exists but `git.read_commit` raises — a blob or a tree id
      where a commit belongs; "not a commit", refused;
    * the successor / relation checks need no gateway and cannot be starved of
      one; what they can lack is the successor's FILE, and a successor whose
      file did not parse counts as absent (its own problem says why).

    Never raises. A loader above this promises the same, and keeps it here.
    """
    loaded = tuple(loaded)
    copies: dict[str, int] = {}
    for entry in loaded:
        copies[entry.record.id] = copies.get(entry.record.id, 0) + 1
    declared = set(copies)
    verdicts = _verify_commits(loaded, git)
    accepted: list[LoadedRecord] = []
    problems: list[RecordProblem] = []
    for entry in loaded:
        record = entry.record
        found: list[str] = []
        if record.last_verified_commit:
            reason = verdicts.get(record.last_verified_commit, "")
            if reason:
                found.append(
                    f"last_verified_commit {record.last_verified_commit} {reason} "
                    "— a citation that cannot be resolved is refused, not accepted"
                )
        successor = record.superseded_by
        if successor:
            if successor == record.id:
                found.append(
                    f"superseded_by names the record itself ({record.id!r}), which "
                    "retires it into nothing anybody can read instead"
                )
            elif successor not in declared:
                found.append(
                    f"superseded_by names {successor!r}, and no record file in this "
                    "load declares that id — a supersession nobody can follow"
                )
        for related in record.related_ids:
            if related not in declared:
                found.append(
                    f"related_ids names {related!r}, and no record file in this "
                    "load declares that id — a relation the resolver could never "
                    "follow"
                )
        if found:
            problems.extend(RecordProblem(entry.source, message) for message in found)
            if copies[record.id] == 1:
                continue
            # A copy of a duplicated id falls through: see the docstring. The
            # index refuses the id under every file, so nothing wins here.
        accepted.append(entry)
    return tuple(accepted), tuple(problems)


def _verify_commits(loaded, git) -> dict[str, str]:
    """`{commit: reason}` for every DISTINCT non-empty `last_verified_commit`
    among `loaded` that does not resolve in `git`; a resolving commit has no
    entry. `git is None` is a reason for all of them, and no probe is made."""
    commits = sorted({e.record.last_verified_commit for e in loaded if e.record.last_verified_commit})
    verdicts: dict[str, str] = {}
    if not commits:
        return verdicts
    if git is None:
        for commit in commits:
            verdicts[commit] = (
                "could not be verified: no repository was given to resolve it in"
            )
        return verdicts
    for commit in commits:
        try:
            exists = git.object_exists(commit)
        except _GATEWAY_FAILURES as exc:
            verdicts[commit] = (
                f"could not be verified: the repository gateway did not answer "
                f"({type(exc).__name__}: {_one_line(exc)})"
            )
            continue
        if exists is not True:
            # `False` is git's own "not here"; anything else is an object that is
            # not a gateway answering the question asked, which is a starved
            # check and reads the same way.
            verdicts[commit] = (
                "does not exist in the repository it was resolved in"
                if exists is False
                else f"could not be verified: the gateway answered {exists!r} instead of True/False"
            )
            continue
        try:
            # `cat-file commit`: dies for a blob or a tree id written where a
            # commit belongs, and dereferences a tag to the commit under it —
            # which is exactly the set of ids the resolver can take a tree of.
            git.read_commit(commit)
        except _GATEWAY_FAILURES as exc:
            verdicts[commit] = f"exists but is not a commit ({_one_line(exc)})"
    return verdicts


def _parse_record_bytes(name: str, raw: bytes) -> LoadedRecord | RecordProblem:
    """The ONE step from a file's bytes to a record or a named problem, shared
    by both loaders so a disk and a blob holding the same bytes load alike.

    `UnicodeDecodeError` is caught here and nowhere else: it is a `ValueError`,
    not an `OSError`, so a record file that is not UTF-8 would otherwise escape
    a loader that only guards the read — and take every other record with it.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return RecordProblem(name, f"not UTF-8: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return RecordProblem(name, f"not valid JSON: {exc}")
    try:
        return LoadedRecord(record_from_mapping(data), name)
    except ContextRecordError as exc:
        return RecordProblem(name, str(exc))


def _collect(parsed, records: list, problems: list) -> None:
    (records if isinstance(parsed, LoadedRecord) else problems).append(parsed)


def _one_line(text) -> str:
    """Collapse whitespace so a git error occupies one rendered line — the
    same rule `context_packet._one_line` applies to everything it renders, kept
    here so this module owes that one nothing."""
    return " ".join(str(text).split())
