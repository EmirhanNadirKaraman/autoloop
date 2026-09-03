"""Every context record, loaded ONCE, indexed by id and by source path.

The resolver above this asks two questions over and over — "which record is
`ctx-feature-7`?" and "which records assert about `autoloop/policy.py`?" — and
both have to be answered without re-reading a directory per question. That is
the whole of this module: one load, two mappings, and one honest account of
what could not be indexed.

**A DUPLICATE ID IS NOT RESOLVED HERE, AND NOT ANYWHERE.** Two files declaring
one id are BOTH excluded from `by_id` and from `by_source_path`, and the id is
reported in `duplicate_ids` naming every file that declared it. Letting one win
— last file read, longest record, whatever — is the failure this is written
against: the selection would carry a record the seed list did not mean, with a
provenance line saying it was named directly, and nothing anywhere would say a
choice had been made. The resolver reports a reference to such an id as its own
category, distinct from an id the index has never heard of, because "you have
two of these" and "you have none of these" are different repairs.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .context_records import ContextRecord, LoadedRecord, RecordProblem, load_records


@dataclass(frozen=True)
class ContextIndex:
    """The loaded records and the two lookups over them.

    Every mapping is read-only (`MappingProxyType`) and every sequence is
    sorted, so an index is a value: two loads of one directory produce two
    indexes that render identically, and nothing downstream can mutate the
    thing it was handed.
    """

    #: Every UNAMBIGUOUS record, in `ContextRecord.order_key` order.
    records: tuple[ContextRecord, ...]
    #: id -> record, duplicates excluded.
    by_id: Mapping[str, ContextRecord]
    #: source path -> the ids asserting about it, sorted; duplicates excluded.
    by_source_path: Mapping[str, tuple[str, ...]]
    #: id -> the file names that declared it, sorted. Never empty for a key,
    #: and every key here is absent from `by_id`.
    duplicate_ids: Mapping[str, tuple[str, ...]]
    #: Files that could not be read at all, in `RecordProblem.order_key` order.
    problems: tuple[RecordProblem, ...]

    def get(self, record_id: str) -> ContextRecord | None:
        return self.by_id.get(record_id)

    def is_duplicated(self, record_id: str) -> bool:
        return record_id in self.duplicate_ids


def build_index(
    loaded: tuple[LoadedRecord, ...] | list[LoadedRecord],
    problems: tuple[RecordProblem, ...] | list[RecordProblem] = (),
) -> ContextIndex:
    """The pure half: records in, index out. No filesystem, no git.

    Separate from `load_index` so a test — and ctx-04's caller, which may hold
    records from somewhere else entirely — can state the records it means
    instead of writing forty files to say it.
    """
    by_source: dict[str, list[str]] = {}
    sources_by_id: dict[str, list[str]] = {}
    records_by_id: dict[str, ContextRecord] = {}
    for entry in loaded:
        sources_by_id.setdefault(entry.record.id, []).append(entry.source)
        # Keep the FIRST record for an id only so that `records_by_id` has
        # something to drop when the id turns out to be duplicated; it is never
        # used as a winner — see the duplicate filter below.
        records_by_id.setdefault(entry.record.id, entry.record)
    duplicate_ids = {
        record_id: tuple(sorted(sources))
        for record_id, sources in sources_by_id.items()
        if len(sources) > 1
    }
    unambiguous = [
        record
        for record_id, record in records_by_id.items()
        if record_id not in duplicate_ids
    ]
    unambiguous.sort(key=lambda record: record.order_key)
    for record in unambiguous:
        for path in record.source_paths:
            by_source.setdefault(path, []).append(record.id)
    return ContextIndex(
        records=tuple(unambiguous),
        by_id=MappingProxyType({record.id: record for record in unambiguous}),
        by_source_path=MappingProxyType(
            {path: tuple(sorted(ids)) for path, ids in sorted(by_source.items())}
        ),
        duplicate_ids=MappingProxyType(dict(sorted(duplicate_ids.items()))),
        problems=tuple(sorted(problems, key=lambda problem: problem.order_key)),
    )


def load_index(directory) -> ContextIndex:
    """`build_index` over `context_records.load_records(directory)`.

    The ONE place the loop reads a record directory, so "load every record
    once" is a property of the code rather than a convention every caller has
    to remember. A directory that does not exist is an EMPTY index carrying the
    problem that says so, never an exception and never a silent empty.
    """
    loaded, problems = load_records(directory)
    return build_index(loaded, problems)
