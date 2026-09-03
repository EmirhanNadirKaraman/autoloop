"""Deterministic, provenance-carrying, stale-aware context selection.

ONE CLAIM: given a repository state and an explicit seed list of record ids,
`resolve_context` returns the same records in the same order with a recorded
reason for each — and marks a record STALE only when that record's own
`source_paths` differ between its `last_verified_commit` and the checkout it
was resolved against, never merely because HEAD advanced.

**Pure given its inputs**, the way `context.build_context` is: an index, a seed
list, a gateway and a budget in; a value out. It reads no config, no task and
no state file. `Task.context_ids` is ctx-04's; this function takes the seed
list as an argument.

WHAT IT SELECTS. The seeds, and then the transitive closure of `related_ids`
over them — explicit edges only. No similarity search, no embeddings, no
"related because the words look alike"; a record the seed list does not reach
through a relation somebody wrote down is not selected. Edges are DIRECTED as
written: naming B in A's `related_ids` pulls B in when A is selected and does
not pull A in when B is.

STALENESS BY TREE COMPARISON, AND THAT IS DELIBERATE.
`GitGateway.tree_of` + `GitGateway.changed_paths` (`git diff-tree -r
--name-only -z`) answer "did these paths change between the commit this record
was verified at and the checkout we resolved against". `git log <sha>..HEAD --
<paths>` is NOT on the policy allowlist and is not wanted here anyway: a change
made and then reverted leaves the record genuinely still accurate, and a
history walk would call it stale. **Nothing in this module widens
`policy._ALLOWED_GIT`.** The three calls it makes — `tree_of` (`rev-parse`),
`tree_entries` (`ls-tree -r -z`) and `changed_paths` (`diff-tree -r
--name-only -z`) — are each already admitted.

ONE DIFF-TREE PER DISTINCT COMMIT, not one per record: records are grouped by
`last_verified_commit` first, so forty records verified at one commit cost one
comparison. `test_context_resolver.py` counts the real argv rather than
trusting this sentence.

NOTHING IS SILENT, AND NOTHING FAILS OPEN. Every category below is reported and
none of them is a reason to drop a record from the selection:

* `unreadable_record` — a record file the index could not read at all.
* `duplicate_record_id` — an id two files declared. Neither wins.
* `unknown_record` — a referenced id the index does not hold.
* `superseded` — a record that names a successor. Never returned as active.
* `dangling_supersession` — that successor cannot be resolved. The record is
  STILL superseded; see `ContextRecord.superseded_by`.
* `missing_source_path` — a path the record names that does not exist in the
  checkout. Reported, and the record is still selected.
* `contradiction` — two ACTIVE selected records asserting different invariants
  over one source path. Recorded; no winner is picked.
* `stale` / `staleness_unknown` — the two halves of the tri-state below.
* `budget_dropped` — what the budget removed, and by which rule.

STALENESS IS A TRI-STATE, and that is the anti-fail-open of this file. A
boolean `stale` defaulting to False would satisfy "marks STALE only when the
paths differ" vacuously while reporting a record whose paths DID change as
fine, because its commit no longer resolves. So a record whose
`last_verified_commit` is empty, or does not resolve in this checkout, is
`STALENESS_UNKNOWN` — never `FRESH` — and says so in a finding.

The one thing that DOES raise is the resolution checkout itself: if `rev` will
not resolve, no record can be verified against anything and answering at all
would be a fabricated verdict. `ContextResolutionError`, named and fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .context_index import ContextIndex
from .context_records import ContextRecord
from .errors import GitError

#: Staleness, as three values rather than a boolean. See the module docstring.
FRESH = "fresh"
STALE = "stale"
STALENESS_UNKNOWN = "unknown"

#: Finding categories. One string per category, so a caller can filter on a
#: constant instead of matching prose.
UNREADABLE_RECORD = "unreadable_record"
DUPLICATE_RECORD_ID = "duplicate_record_id"
UNKNOWN_RECORD = "unknown_record"
SUPERSEDED = "superseded"
DANGLING_SUPERSESSION = "dangling_supersession"
MISSING_SOURCE_PATH = "missing_source_path"
CONTRADICTION = "contradiction"
STALE_FINDING = "stale"
STALENESS_UNKNOWN_FINDING = "staleness_unknown"
BUDGET_DROPPED = "budget_dropped"

#: The reason a seed carries. Every seed carries the SAME one, whatever its
#: position in the list, which is half of why reversing the seed list cannot
#: change the output.
SEED_REASON = "seed reference: named directly in the seed list"


class ContextResolutionError(Exception):
    """The checkout a resolution was asked for cannot be read.

    Fail-closed on purpose: with no tree to compare against, every staleness
    verdict and every existence check would be invented. Distinct from a
    RECORD's commit failing to resolve, which is per-record data, is expected
    (a rebased or garbage sha) and produces `STALENESS_UNKNOWN` rather than
    destroying the whole resolution.
    """


def _one_line(text) -> str:
    """Collapse whitespace so one finding occupies exactly one line.

    The same helper, for the same reason, as `context._one_line`: findings
    interpolate record titles, invariants and `GitError` messages, all of which
    can be multi-line, and a line-oriented block whose lines are not lines is
    how a reader gets confused. Duplicated rather than imported because
    `context` reaches state, git and the task registry, and this module
    deliberately reaches almost nothing.
    """
    return " ".join(str(text).split())


@dataclass(frozen=True)
class SelectedRecord:
    """One record in the selection, with WHY it is there and how fresh it is."""

    record: ContextRecord
    #: One line: `SEED_REASON`, or which record related it in.
    reason: str
    #: 0 for a seed, 1 for a record a seed relates to, and so on. Carried
    #: because it is the first component of the budget's drop rule, so a
    #: reviewer can reproduce that rule from the output.
    depth: int
    #: `FRESH`, `STALE` or `STALENESS_UNKNOWN`.
    staleness: str

    @property
    def order_key(self) -> tuple[str, str]:
        return self.record.order_key


@dataclass(frozen=True)
class Finding:
    """One reported category, about one subject, in one line."""

    category: str
    #: What the finding is about: the record id, normally. For `CONTRADICTION`
    #: it is the SOURCE PATH, because a contradiction is a fact about a path
    #: and naming either of the two records would read as having picked one;
    #: for `UNREADABLE_RECORD` it is the file name, because a file that would
    #: not parse has no id anybody can trust.
    subject: str
    detail: str

    @property
    def order_key(self) -> tuple[str, str, str]:
        return (self.category, self.subject, self.detail)


@dataclass(frozen=True)
class Resolution:
    """What `resolve_context` answers with.

    `selected` is in `ContextRecord.order_key` order (kind, then id) and
    `findings` in `Finding.order_key` order, so neither depends on dict
    insertion, filesystem iteration or the order the seeds were written in.
    """

    selected: tuple[SelectedRecord, ...]
    findings: tuple[Finding, ...]
    #: The revision resolved against, as asked for, and the tree it named.
    #: Provenance: two resolutions with the same tree saw the same files.
    rev: str
    tree: str
    max_records: int

    def findings_of(self, category: str) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.category == category)


@dataclass
class _Candidate:
    """A selected record before the budget and the verification have run.

    Mutable and private: `SelectedRecord` is what leaves this module, and it is
    frozen. Staleness cannot be filled in at selection time because it is
    computed only for what SURVIVES the budget.
    """

    record: ContextRecord
    reason: str
    depth: int


def _supersession_finding(index: ContextIndex, record: ContextRecord) -> Finding | None:
    """The extra finding a superseded record earns when its successor cannot be
    resolved. `None` when the successor is an ordinary, resolvable record.

    This NEVER changes whether the record is superseded — that is decided by
    `superseded_by` being non-empty and nothing else. A dangling successor that
    made a retired record active again would invert the one guarantee this
    category exists for.
    """
    successor = record.superseded_by
    if successor == record.id:
        detail = "names itself as its own successor"
    elif index.is_duplicated(successor):
        detail = (
            f"its successor {successor!r} is declared by more than one file "
            f"({', '.join(index.duplicate_ids[successor])}) and is not indexed"
        )
    elif successor not in index.by_id:
        detail = f"the index holds no record with the successor id {successor!r}"
    else:
        return None
    return Finding(
        DANGLING_SUPERSESSION,
        record.id,
        f"{detail} — the record is superseded either way and stays out of the "
        "active selection",
    )


def _select(
    index: ContextIndex, seed_ids: Iterable[str], findings: list[Finding]
) -> list[_Candidate]:
    """Seeds, then the transitive closure of `related_ids`, breadth first.

    DETERMINISM, in three parts:

    * every seed carries the same reason, so seed ORDER cannot reach the
      output through it;
    * each frontier is expanded in `order_key` order, so which record "related
      in" a record reachable from two of them is decided by (kind, id) and not
      by who happened to be listed first;
    * an id is visited at most once, which both terminates a relation cycle and
      keeps a record referenced twice from earning two findings.
    """
    visited: set[str] = set()
    candidates: list[_Candidate] = []

    def consider(record_id: str, reason: str, depth: int, referrer: str) -> _Candidate | None:
        if record_id in visited:
            return None
        visited.add(record_id)
        if index.is_duplicated(record_id):
            findings.append(
                Finding(
                    DUPLICATE_RECORD_ID,
                    record_id,
                    f"{referrer}, and this id is declared by "
                    f"{', '.join(index.duplicate_ids[record_id])} — no winner is "
                    "picked, so nothing under this id is selected",
                )
            )
            return None
        record = index.get(record_id)
        if record is None:
            findings.append(
                Finding(
                    UNKNOWN_RECORD,
                    record_id,
                    f"{referrer}, and the index holds no record with this id",
                )
            )
            return None
        if record.is_superseded:
            findings.append(
                Finding(
                    SUPERSEDED,
                    record_id,
                    f"{referrer}, and it is superseded by "
                    f"{record.superseded_by!r} — never returned as active, and "
                    "not expanded through",
                )
            )
            dangling = _supersession_finding(index, record)
            if dangling is not None:
                findings.append(dangling)
            return None
        candidate = _Candidate(record=record, reason=reason, depth=depth)
        candidates.append(candidate)
        return candidate

    frontier: list[_Candidate] = []
    for seed_id in seed_ids:
        picked = consider(seed_id, SEED_REASON, 0, "named in the seed list")
        if picked is not None:
            frontier.append(picked)
    depth = 1
    while frontier:
        frontier.sort(key=lambda candidate: candidate.record.order_key)
        next_frontier: list[_Candidate] = []
        for parent in frontier:
            name = f"{parent.record.kind}/{parent.record.id}"
            for related_id in parent.record.related_ids:
                picked = consider(
                    related_id,
                    f"related in by {name} at depth {depth}",
                    depth,
                    f"referenced by {name}",
                )
                if picked is not None:
                    next_frontier.append(picked)
        frontier = next_frontier
        depth += 1
    return candidates


def _apply_budget(
    candidates: list[_Candidate], max_records: int, findings: list[Finding]
) -> list[_Candidate]:
    """Keep the first `max_records` by (depth, kind, id); report the rest.

    THE RULE IS STATED, AND IT IS STATED IN THE OUTPUT. Silent truncation is
    the failure being prevented here, so every dropped record earns a finding
    naming the budget, its own rank and the order that ranked it.

    Depth leads deliberately: a seed the caller named explicitly must not be
    dropped in favour of a record three relations away that happens to sort
    earlier by kind. Within a depth the order is the same total (kind, id) the
    selection is returned in, so the rule is reproducible from the output.
    """
    ranked = sorted(
        candidates,
        key=lambda candidate: (candidate.depth, candidate.record.kind, candidate.record.id),
    )
    kept = ranked[:max_records]
    for position, candidate in enumerate(ranked[max_records:], start=max_records + 1):
        findings.append(
            Finding(
                BUDGET_DROPPED,
                candidate.record.id,
                f"context budget max_records={max_records} binds: this record "
                f"ranked {position} of {len(ranked)} by (depth from seed, kind, "
                f"id) at depth {candidate.depth} — {_one_line(candidate.reason)}",
            )
        )
    return kept


def _verify_staleness(
    kept: list[_Candidate], git, rev: str, head_tree: str, findings: list[Finding]
) -> dict[str, str]:
    """`{record id: FRESH | STALE | STALENESS_UNKNOWN}`, one diff-tree per
    distinct `last_verified_commit`.

    A record with NO source paths and a resolvable commit is FRESH, and that is
    decided rather than overlooked: the rule is "stale when this record's own
    source paths differ", and no paths can never differ. It asserts nothing
    about files, so nothing about files can invalidate it.

    A commit that will not resolve is `STALENESS_UNKNOWN` for every record
    verified at it, with the git error on the finding. It is never FRESH: "the
    check could not run" and "the check ran and found nothing" are the two
    answers this file exists to keep apart.

    A path that was DELETED between the two commits is caught here — a deletion
    is a change `diff-tree` reports, so the record goes STALE. A path that
    existed at NEITHER end (a typo, or a directory name where git only records
    files) changes in no comparison and so reads FRESH; that is why
    `_report_missing` is its own reported category rather than a footnote on
    this one. Staleness answers "did these paths move under the record", and
    only that.
    """
    staleness: dict[str, str] = {}
    by_commit: dict[str, list[ContextRecord]] = {}
    for candidate in kept:
        commit = candidate.record.last_verified_commit
        if not commit:
            staleness[candidate.record.id] = STALENESS_UNKNOWN
            findings.append(
                Finding(
                    STALENESS_UNKNOWN_FINDING,
                    candidate.record.id,
                    "the record names no last_verified_commit, so it has never "
                    "been verified against any tree and cannot be reported fresh",
                )
            )
            continue
        by_commit.setdefault(commit, []).append(candidate.record)
    for commit in sorted(by_commit):
        try:
            changed = git.changed_paths(git.tree_of(commit), head_tree)
        except GitError as exc:
            for record in by_commit[commit]:
                staleness[record.id] = STALENESS_UNKNOWN
                findings.append(
                    Finding(
                        STALENESS_UNKNOWN_FINDING,
                        record.id,
                        f"last_verified_commit {commit} does not resolve in this "
                        f"checkout ({_one_line(exc)}), so its paths could not be "
                        "compared and it is not reported fresh",
                    )
                )
            continue
        for record in by_commit[commit]:
            differing = sorted(path for path in record.source_paths if path in changed)
            if not differing:
                staleness[record.id] = FRESH
                continue
            staleness[record.id] = STALE
            findings.append(
                Finding(
                    STALE_FINDING,
                    record.id,
                    f"its own source paths changed between {commit} and {rev}: "
                    + ", ".join(differing),
                )
            )
    return staleness


def _report_missing(
    kept: list[_Candidate], tree_paths: set[str], rev: str, findings: list[Finding]
) -> None:
    """A named source path absent from the resolution tree is REPORTED, never
    dropped and never a reason to drop the record that names it.

    Against the TREE, never `Path.exists()`: the filesystem answer depends on
    whatever is uncommitted in the working directory, which would make one
    repository state produce two different selections.
    """
    for candidate in kept:
        for path in sorted(candidate.record.source_paths):
            if path in tree_paths:
                continue
            findings.append(
                Finding(
                    MISSING_SOURCE_PATH,
                    candidate.record.id,
                    f"names {path}, which does not exist in {rev} — the record "
                    "is still selected and still carries its claim",
                )
            )


def _report_contradictions(kept: list[_Candidate], findings: list[Finding]) -> None:
    """Two ACTIVE selected records asserting different invariants over one
    source path: RECORDED, with both, and no winner picked.

    Over the KEPT set — what the resolution actually returns — so every finding
    concerns a record the caller can see, and anything the budget removed is
    accounted for by its own `budget_dropped` finding rather than by silence.

    A record with an empty `invariant` asserts nothing checkable and so can
    contradict nothing; two records asserting the SAME invariant agree, which
    is the ordinary case for a feature and the lesson written against it.
    """
    by_path: dict[str, list[ContextRecord]] = {}
    for candidate in kept:
        if not candidate.record.invariant:
            continue
        for path in candidate.record.source_paths:
            by_path.setdefault(path, []).append(candidate.record)
    for path in sorted(by_path):
        group = by_path[path]
        if len({record.invariant for record in group}) < 2:
            continue
        stated = sorted(
            f"{record.kind}/{record.id} asserts {_one_line(record.invariant)!r}"
            for record in group
        )
        findings.append(
            Finding(
                CONTRADICTION,
                path,
                f"{len(group)} active selected records assert different "
                "invariants over this path: " + "; ".join(stated) + " — recorded, "
                "not resolved",
            )
        )


def resolve_context(
    index: ContextIndex,
    seed_ids: Iterable[str],
    git,
    *,
    max_records: int,
    rev: str = "HEAD",
) -> Resolution:
    """THE selection. See the module docstring for the claim it carries.

    `max_records` is REQUIRED and has no default here, deliberately: the
    default belongs to `[context] max_records` in the config, and a resolver
    that silently defaulted would be an unbounded selection wearing a budget's
    clothes. Values below 1 are refused rather than clamped, in
    `[concurrency] lanes`' style — a budget of zero selects nothing while
    reading as configured.
    """
    if isinstance(max_records, bool) or not isinstance(max_records, int):
        raise ValueError(
            f"max_records must be an integer number of records, got {max_records!r}"
        )
    if max_records < 1:
        raise ValueError(
            f"max_records must be at least 1, got {max_records!r} — a budget of "
            "zero returns nothing while reading as configured"
        )
    findings: list[Finding] = []
    for problem in index.problems:
        findings.append(
            Finding(
                UNREADABLE_RECORD,
                problem.source,
                _one_line(problem.message)
                + " — this file's record is in no index and can be selected by "
                "nothing",
            )
        )
    # Every duplicate in the index, not only the ones this seed list reached: a
    # duplicated id makes the whole index ambiguous, and a selection that stayed
    # quiet about one because it happened not to touch it is the same silence
    # this module is written against.
    for record_id, sources in index.duplicate_ids.items():
        findings.append(
            Finding(
                DUPLICATE_RECORD_ID,
                record_id,
                f"declared by {', '.join(sources)} — indexed under neither, so "
                "no record wins this id",
            )
        )
    try:
        head_tree = git.tree_of(rev)
        tree_paths = set(git.tree_entries(head_tree))
    except GitError as exc:
        raise ContextResolutionError(
            f"cannot resolve context against {rev!r}: {_one_line(exc)}. Nothing "
            "was selected — with no tree to compare against, every staleness "
            "verdict would be invented."
        ) from exc

    candidates = _select(index, seed_ids, findings)
    kept = _apply_budget(candidates, max_records, findings)
    staleness = _verify_staleness(kept, git, rev, head_tree, findings)
    _report_missing(kept, tree_paths, rev, findings)
    _report_contradictions(kept, findings)

    selected = tuple(
        sorted(
            (
                SelectedRecord(
                    record=candidate.record,
                    reason=candidate.reason,
                    depth=candidate.depth,
                    # `STALENESS_UNKNOWN` is the fallback rather than `FRESH`
                    # for the reason the module docstring gives: an unanswered
                    # question is never reported as a clean answer.
                    staleness=staleness.get(candidate.record.id, STALENESS_UNKNOWN),
                )
                for candidate in kept
            ),
            key=lambda item: item.order_key,
        )
    )
    return Resolution(
        selected=selected,
        findings=tuple(sorted(findings, key=lambda finding: finding.order_key)),
        rev=rev,
        tree=head_tree,
        max_records=max_records,
    )


def render_resolution(resolution: Resolution) -> str:
    """The resolution as a line-oriented block, one line per item.

    Byte-identical for two resolutions of one repository state and one seed
    list — that is what `test_context_resolver.py` compares, because a claim
    about determinism that is only asserted over field values leaves the
    rendering free to depend on iteration order.
    """
    lines = [
        f"context: resolved against {resolution.rev} (tree {resolution.tree}) — "
        f"{len(resolution.selected)} selected, {len(resolution.findings)} "
        f"findings, budget max_records={resolution.max_records}"
    ]
    for item in resolution.selected:
        title = _one_line(item.record.title) or "(no title)"
        lines.append(
            f"  selected: {item.record.kind}/{item.record.id} "
            f"[{item.staleness}] — {title} — {item.reason}"
        )
    for finding in resolution.findings:
        subject = finding.subject or "(none)"
        lines.append(f"  finding: {finding.category} — {subject} — {finding.detail}")
    return "\n".join(lines)
