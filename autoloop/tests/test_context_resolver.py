"""Context selection is deterministic, carries its provenance, and is stale-aware.

THE CLAIM under test (ctx-03): given a repository state and an explicit seed
list of context ids, `context_resolver.resolve_context` returns the same
records in the same order with a recorded reason for each — and marks a record
STALE only when that record's own `source_paths` differ between its
`last_verified_commit` and the checkout it was resolved against, never merely
because HEAD advanced.

REAL GIT THROUGHOUT for everything that is a claim about git — staleness, the
made-and-reverted case, the missing-path check and the one-diff-tree-per-commit
budget are all claims about what `diff-tree` and `ls-tree` actually answer, and
a fake gateway would pin this file's opinion of git rather than git. The record,
index and budget claims are pure functions and are exercised as such: they get
`build_index` over records stated in the test, not forty files on disk.

WHAT IS DELIBERATELY NOT HERE. `Task.context_ids` (ctx-04 wires it), packets and
prompts. The resolver takes its seed list as an argument and stays pure the way
`context.build_context` is pure given its inputs.
"""

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
from gitrepo import make_repo_from_template, run_git

from autoloop.config import (
    DEFAULT_CONTEXT_MAX_RECORDS,
    AutoloopConfig,
    ContextConfig,
    load_config,
)
from autoloop.context_index import build_index, load_index
from autoloop.context_records import (
    RECORD_KINDS,
    ContextRecord,
    ContextRecordError,
    LoadedRecord,
    load_records,
    record_from_mapping,
)
from autoloop.context_resolver import (
    BUDGET_DROPPED,
    CONTRADICTION,
    DANGLING_SUPERSESSION,
    DUPLICATE_RECORD_ID,
    FRESH,
    MISSING_SOURCE_PATH,
    SEED_REASON,
    STALE,
    STALE_FINDING,
    STALENESS_UNKNOWN,
    STALENESS_UNKNOWN_FINDING,
    SUPERSEDED,
    UNKNOWN_RECORD,
    UNREADABLE_RECORD,
    ContextResolutionError,
    render_resolution,
    resolve_context,
)
from autoloop.errors import ConfigError
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.toml"

#: The two files every repository fixture below starts with, so a record can
#: name one of them and a commit can touch the other.
BASE_FILES = (("a.py", "a\n"), ("b.py", "b\n"))


class CountingRunner:
    """`subprocess.run`, plus a record of every argv it was handed.

    The real runner, so every git call in a test using this is a real git call:
    the one-diff-tree-per-commit claim is about the commands the resolver
    ISSUES, and monkeypatching `changed_paths` would count the calls this test
    file made rather than the ones git saw.
    """

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        return subprocess.run(argv, **kwargs)

    def count(self, verb: str) -> int:
        return sum(1 for argv in self.calls if len(argv) > 1 and argv[1] == verb)


def make_git(root, runner=None) -> GitGateway:
    return GitGateway(Path(root), PolicyEngine(PolicyConfig()), runner=runner)


def commit(root, path: str, body: str, message: str = "change") -> str:
    """Write `path`, commit it, return the new HEAD sha."""
    target = Path(root) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", message)
    return run_git(root, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    """A repository holding `a.py` and `b.py` at one commit."""
    root = tmp_path / "repo"
    make_repo_from_template(root, files=BASE_FILES)
    return root


def head(root) -> str:
    return run_git(root, "rev-parse", "HEAD").strip()


def rec(record_id: str, kind: str = "feature", **fields) -> ContextRecord:
    return ContextRecord(id=record_id, kind=kind, **fields)


def index_of(*records: ContextRecord, problems=()):
    """`build_index` over records stated inline, one notional file each."""
    return build_index(
        [LoadedRecord(record, f"{record.id}.json") for record in records], problems
    )


def selected_ids(resolution) -> list[str]:
    return [item.record.id for item in resolution.selected]


def subjects(resolution, category: str) -> list[str]:
    return [finding.subject for finding in resolution.findings_of(category)]


# ---- 1. the record and the index --------------------------------------------


def test_a_duplicate_id_is_reported_and_neither_record_wins():
    """Reporting a duplicate rather than letting one win means BOTH are out of
    every lookup, and the id names the files that declared it.

    Letting one win would put a record the seed list did not mean into the
    selection, with a provenance line saying it was named directly, and nothing
    anywhere would say a choice had been made."""
    first = rec("dup", source_paths=("a.py",))
    second = rec("dup", kind="lesson", source_paths=("b.py",))
    index = build_index(
        [LoadedRecord(first, "one.json"), LoadedRecord(second, "two.json")]
    )

    assert index.by_id == {}
    assert index.records == ()
    assert index.duplicate_ids["dup"] == ("one.json", "two.json")
    assert index.by_source_path == {}, "a duplicated record must index no path either"
    assert index.is_duplicated("dup")


def test_the_index_answers_both_questions_in_a_total_order():
    """One load, two lookups, and every sequence sorted: an index is a value,
    so two loads of one directory render identically."""
    index = index_of(
        rec("z", kind="decision", source_paths=("a.py", "b.py")),
        rec("a", kind="lesson", source_paths=("a.py",)),
        rec("m", kind="decision", source_paths=("b.py",)),
    )

    # kind, then id — the same total order `next_ready`'s id tiebreak carries.
    assert [record.id for record in index.records] == ["m", "z", "a"]
    assert index.by_source_path["a.py"] == ("a", "z")
    assert index.by_source_path["b.py"] == ("m", "z")
    assert index.get("nope") is None


def test_a_record_file_that_will_not_parse_becomes_a_problem_not_a_gap(tmp_path):
    """Tolerant, never silent. One bad file must not take the good ones with
    it, and must not disappear either — a selection missing the one record that
    contradicted it, saying nothing, is the fail-open this whole file is
    written against."""
    directory = tmp_path / "records"
    directory.mkdir()
    (directory / "good.json").write_text(
        json.dumps({"id": "good", "kind": "feature"}), encoding="utf-8"
    )
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    (directory / "typo.json").write_text(
        json.dumps({"id": "typo", "kind": "feature", "source_path": "a.py"}),
        encoding="utf-8",
    )
    (directory / "kind.json").write_text(
        json.dumps({"id": "kind", "kind": "anecdote"}), encoding="utf-8"
    )
    (directory / "notes.md").write_text("not a record", encoding="utf-8")

    index = load_index(directory)

    assert [record.id for record in index.records] == ["good"]
    reported = {problem.source for problem in index.problems}
    assert reported == {"broken.json", "typo.json", "kind.json"}
    assert index.problems[0].source == "broken.json", "problems are name-ordered"
    assert "source_path" in "".join(problem.message for problem in index.problems)


def test_a_missing_record_directory_is_one_reported_problem(tmp_path):
    """An empty index that says WHY it is empty. Raising would destroy a round
    over a directory nobody has created yet; an empty index that said nothing
    would answer every question with a confident "no record"."""
    records, problems = load_records(tmp_path / "nope")

    assert records == ()
    assert len(problems) == 1
    assert "does not exist" in problems[0].message


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "feature"},
        {"id": "x"},
        {"id": " x ", "kind": "feature"},
        {"id": "x", "kind": "feature", "source_paths": "a.py"},
        {"id": "x", "kind": "feature", "source_paths": ["/etc/passwd"]},
        {"id": "x", "kind": "feature", "source_paths": ["../outside.py"]},
        {"id": "x", "kind": "feature", "source_paths": ["./a.py"]},
        {"id": "x", "kind": "feature", "related_ids": [""]},
        {"id": "x", "kind": "feature", "title": 7},
    ],
)
def test_a_record_that_could_never_match_is_refused_at_parse_time(data):
    """Each of these would otherwise load as a claim that can never be found
    stale, missing or contradictory — a bare string read as five one-character
    paths, a padded id no seed list will match, a path git can never name."""
    with pytest.raises(ContextRecordError):
        record_from_mapping(data)


def test_every_kind_the_resolver_expands_to_is_a_kind_a_record_may_have():
    """The four kinds the task names, and no fifth: a new kind changes what the
    resolver selects and belongs in a reviewed commit, not in a data file."""
    assert set(RECORD_KINDS) == {"decision", "feature", "incident", "lesson"}
    for kind in RECORD_KINDS:
        assert record_from_mapping({"id": "x", "kind": kind}).kind == kind


# ---- 2. determinism ----------------------------------------------------------


def test_the_same_state_and_seeds_render_byte_identically_twice(repo):
    """Determinism is a test, not an aspiration — and rendered, not compared
    field by field, because a claim asserted over field values leaves the
    rendering free to depend on iteration order."""
    index = index_of(
        rec("f2", source_paths=("a.py",), related_ids=("l1",), last_verified_commit=head(repo)),
        rec("f1", source_paths=("b.py",), related_ids=("l1", "d1")),
        rec("l1", kind="lesson", source_paths=("a.py",)),
        rec("d1", kind="decision", source_paths=("nope.py",)),
    )
    git = make_git(repo)

    first = resolve_context(index, ["f1", "f2"], git, max_records=10)
    second = resolve_context(index, ["f1", "f2"], git, max_records=10)

    assert render_resolution(first) == render_resolution(second)
    assert first == second


def test_reversing_the_seed_list_changes_nothing(repo):
    """Two runs of one list catch nothing. Reversing it is what catches order
    leaking into reason attribution: `l1` is reachable from both seeds, so the
    record that "related it in" must be decided by (kind, id) and not by which
    seed the caller happened to write first."""
    index = index_of(
        rec("f1", source_paths=("a.py",), related_ids=("l1",)),
        rec("f2", source_paths=("b.py",), related_ids=("l1",)),
        rec("l1", kind="lesson", related_ids=("d1",)),
        rec("d1", kind="decision"),
    )
    git = make_git(repo)

    forward = resolve_context(index, ["f1", "f2"], git, max_records=10)
    backward = resolve_context(index, ["f2", "f1"], git, max_records=10)

    assert render_resolution(forward) == render_resolution(backward)
    reasons = {item.record.id: item.reason for item in forward.selected}
    assert reasons["l1"] == "related in by feature/f1 at depth 1"
    assert reasons["d1"] == "related in by lesson/l1 at depth 2"


def test_an_empty_seed_list_selects_nothing_and_does_not_raise(repo):
    """The boundary. No seeds is a real question with a real answer, and the
    answer is an empty selection rather than "everything" or an exception."""
    index = index_of(rec("f1"), rec("f2"))

    resolution = resolve_context(index, [], make_git(repo), max_records=10)

    assert resolution.selected == ()
    assert resolution.findings == ()
    assert render_resolution(resolution).startswith("context: resolved against HEAD")


def test_a_relation_cycle_terminates_and_a_repeated_seed_is_selected_once(repo):
    """`a -> b -> a`, plus the same seed twice. A visited set is what makes
    both true, and both are ways a resolver stops being a function."""
    index = index_of(
        rec("a", related_ids=("b",)),
        rec("b", kind="lesson", related_ids=("a",)),
    )

    resolution = resolve_context(index, ["a", "a", "b"], make_git(repo), max_records=10)

    assert selected_ids(resolution) == ["a", "b"]
    assert [item.reason for item in resolution.selected] == [SEED_REASON, SEED_REASON]


# ---- 3. staleness, by tree comparison ----------------------------------------


def test_head_advancing_alone_is_not_stale_but_touching_its_own_paths_is(repo):
    """The claim's core sentence, as one fixture and two verdicts.

    A record verified at C1 over `a.py` is FRESH after a commit that touches
    only `b.py` — HEAD advanced, the record's own paths did not — and STALE
    once `a.py` itself changes."""
    verified = head(repo)
    index = index_of(rec("f1", source_paths=("a.py",), last_verified_commit=verified))
    git = make_git(repo)

    commit(repo, "b.py", "b changed\n")
    after_unrelated = resolve_context(index, ["f1"], git, max_records=10)

    commit(repo, "a.py", "a changed\n")
    after_own = resolve_context(index, ["f1"], git, max_records=10)

    assert after_unrelated.selected[0].staleness == FRESH
    assert after_unrelated.findings_of(STALE_FINDING) == ()
    assert after_own.selected[0].staleness == STALE
    assert subjects(after_own, STALE_FINDING) == ["f1"]
    assert "a.py" in after_own.findings_of(STALE_FINDING)[0].detail


def test_a_change_made_and_then_reverted_leaves_the_record_fresh(repo):
    """Why tree comparison is the more correct answer rather than a shortcut
    around the git allowlist: a change made and reverted leaves the record
    genuinely still accurate. `git log <sha>..HEAD -- <paths>` would call this
    stale, and is not on the allowlist either."""
    verified = head(repo)
    commit(repo, "a.py", "a changed\n")
    commit(repo, "a.py", "a\n", "revert")
    index = index_of(rec("f1", source_paths=("a.py",), last_verified_commit=verified))

    resolution = resolve_context(index, ["f1"], make_git(repo), max_records=10)

    assert resolution.selected[0].staleness == FRESH
    assert resolution.findings == ()


def test_a_commit_that_does_not_resolve_is_unknown_and_never_fresh(repo):
    """The fail-open this design exists to refuse. A boolean `stale` defaulting
    to False would report a record whose paths DID change as fine, because its
    commit no longer resolves — the alarm never fires and nothing says so."""
    index = index_of(rec("f1", source_paths=("a.py",), last_verified_commit="0" * 40))

    resolution = resolve_context(index, ["f1"], make_git(repo), max_records=10)

    assert resolution.selected[0].staleness == STALENESS_UNKNOWN
    assert subjects(resolution, STALENESS_UNKNOWN_FINDING) == ["f1"]
    assert "does not resolve" in resolution.findings_of(STALENESS_UNKNOWN_FINDING)[0].detail


def test_a_record_verified_against_nothing_is_unknown_and_never_fresh(repo):
    """The other half of the same refusal: an empty `last_verified_commit` has
    never been checked against any tree, so it cannot be reported fresh."""
    index = index_of(rec("f1", source_paths=("a.py",)))

    resolution = resolve_context(index, ["f1"], make_git(repo), max_records=10)

    assert resolution.selected[0].staleness == STALENESS_UNKNOWN
    assert "never been verified" in resolution.findings_of(STALENESS_UNKNOWN_FINDING)[0].detail


def test_a_deleted_source_path_is_stale_as_well_as_missing(repo):
    """A deletion is a change `diff-tree` reports, so the record goes STALE —
    and the path is reported MISSING too. The two categories answer different
    questions ("did these paths move under the record" and "is what it names
    still there") and a deletion is the case where both fire at once."""
    verified = head(repo)
    (Path(repo) / "a.py").unlink()
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "delete a.py")
    index = index_of(rec("f1", source_paths=("a.py",), last_verified_commit=verified))

    resolution = resolve_context(index, ["f1"], make_git(repo), max_records=10)

    assert resolution.selected[0].staleness == STALE
    assert subjects(resolution, MISSING_SOURCE_PATH) == ["f1"]


def test_a_record_naming_no_source_paths_cannot_go_stale(repo):
    """Decided, not overlooked: the rule is "stale when this record's OWN
    source paths differ", and no paths can never differ."""
    verified = head(repo)
    commit(repo, "a.py", "a changed\n")
    index = index_of(rec("f1", last_verified_commit=verified))

    resolution = resolve_context(index, ["f1"], make_git(repo), max_records=10)

    assert resolution.selected[0].staleness == FRESH


def test_one_diff_tree_per_distinct_commit_not_one_per_record(repo):
    """The stated requirement, counted against the real argv rather than
    trusted: five records verified at two commits cost two comparisons."""
    first = head(repo)
    second = commit(repo, "b.py", "b changed\n")
    commit(repo, "a.py", "a changed\n")
    index = index_of(
        rec("f1", source_paths=("a.py",), last_verified_commit=first),
        rec("f2", source_paths=("a.py",), last_verified_commit=first),
        rec("f3", source_paths=("b.py",), last_verified_commit=first),
        rec("f4", kind="lesson", source_paths=("a.py",), last_verified_commit=second),
        rec("f5", kind="lesson", source_paths=("b.py",), last_verified_commit=second),
    )
    runner = CountingRunner()

    resolution = resolve_context(
        index, ["f1", "f2", "f3", "f4", "f5"], make_git(repo, runner), max_records=10
    )

    assert runner.count("diff-tree") == 2, "one comparison per distinct commit"
    assert len(resolution.selected) == 5
    assert {item.record.id: item.staleness for item in resolution.selected} == {
        "f1": STALE, "f2": STALE, "f3": STALE,
        "f4": STALE, "f5": FRESH,
    }


def test_the_allowlisted_primitives_are_the_only_git_this_uses(repo):
    """No new git primitive and no allowlist widening: `rev-parse`, `ls-tree`
    and `diff-tree` are each already in `policy._ALLOWED_GIT`, and a resolution
    issues nothing else. `policy.py` is deliberately outside this task's
    approved paths, so a fourth verb appearing here is a review boundary being
    crossed rather than a detail."""
    index = index_of(rec("f1", source_paths=("a.py",), last_verified_commit=head(repo)))
    runner = CountingRunner()

    resolve_context(index, ["f1"], make_git(repo, runner), max_records=10)

    assert {argv[1] for argv in runner.calls} == {"rev-parse", "ls-tree", "diff-tree"}


def test_a_checkout_that_will_not_resolve_raises_rather_than_inventing_verdicts(repo):
    """Fail-closed, and the one thing here that raises: with no tree to compare
    against, every staleness verdict and every existence check would be
    invented. Answering "fresh, nothing missing" would be the worst of the
    available answers."""
    index = index_of(rec("f1", source_paths=("a.py",)))

    with pytest.raises(ContextResolutionError) as exc:
        resolve_context(index, ["f1"], make_git(repo), max_records=10, rev="no-such-rev")

    assert "no-such-rev" in str(exc.value)


# ---- 4. the reported categories ----------------------------------------------


def test_a_superseded_record_is_never_returned_as_active(repo):
    """Both ways in — named as a seed, and reached by a relation — and it is
    not expanded through either: `l1` is reachable only via the superseded
    record, so pulling it in would make a retired record's relations live."""
    index = index_of(
        rec("old", superseded_by="new", related_ids=("l1",)),
        rec("new"),
        rec("seedling", kind="decision", related_ids=("old",)),
        rec("l1", kind="lesson"),
    )
    git = make_git(repo)

    as_seed = resolve_context(index, ["old"], git, max_records=10)
    by_relation = resolve_context(index, ["seedling"], git, max_records=10)

    assert selected_ids(as_seed) == []
    assert subjects(as_seed, SUPERSEDED) == ["old"]
    assert selected_ids(by_relation) == ["seedling"], "not expanded through"
    assert subjects(by_relation, SUPERSEDED) == ["old"]


def test_an_unresolvable_successor_leaves_the_record_superseded(repo):
    """Gating "is superseded" on resolving the successor would turn a dangling
    id into a retired record coming back as active — the guarantee above,
    inverted. So the supersession stands and the dangling reference is its own
    reported category."""
    index = index_of(
        rec("gone", superseded_by="never-written"),
        rec("selfish", kind="lesson", superseded_by="selfish"),
    )

    resolution = resolve_context(index, ["gone", "selfish"], make_git(repo), max_records=10)

    assert resolution.selected == ()
    assert sorted(subjects(resolution, SUPERSEDED)) == ["gone", "selfish"]
    assert sorted(subjects(resolution, DANGLING_SUPERSESSION)) == ["gone", "selfish"]
    details = " ".join(f.detail for f in resolution.findings_of(DANGLING_SUPERSESSION))
    assert "names itself" in details and "never-written" in details


def test_a_named_source_path_that_is_absent_is_reported_not_dropped(repo):
    """MISSING is reported and the record keeps its claim, and the check is
    against the TREE rather than the filesystem, so an uncommitted file cannot
    make one repository state produce two selections."""
    (Path(repo) / "untracked.py").write_text("not committed\n", encoding="utf-8")
    index = index_of(
        rec("f1", source_paths=("a.py", "gone.py", "untracked.py"), last_verified_commit=head(repo))
    )

    resolution = resolve_context(index, ["f1"], make_git(repo), max_records=10)

    assert selected_ids(resolution) == ["f1"]
    missing = resolution.findings_of(MISSING_SOURCE_PATH)
    assert [finding.subject for finding in missing] == ["f1", "f1"]
    details = " | ".join(finding.detail for finding in missing)
    assert "names gone.py," in details
    assert "names untracked.py," in details, "the working tree is not the tree"
    assert "a.py," not in details


def test_a_contradiction_records_both_sides_and_picks_no_winner(repo):
    """Two ACTIVE records asserting different invariants over one path. The
    finding names the path and both records; nothing is dropped, reordered or
    resolved."""
    index = index_of(
        rec("f1", source_paths=("a.py",), invariant="a.py is\nnever\nimported"),
        rec("l1", kind="lesson", source_paths=("a.py",), invariant="a.py is imported by b.py"),
        rec("d1", kind="decision", source_paths=("a.py",), invariant="a.py is imported by b.py"),
        rec("i1", kind="incident", source_paths=("a.py",)),
    )

    resolution = resolve_context(
        index, ["f1", "l1", "d1", "i1"], make_git(repo), max_records=10
    )

    assert selected_ids(resolution) == ["d1", "f1", "i1", "l1"]
    conflicts = resolution.findings_of(CONTRADICTION)
    assert [finding.subject for finding in conflicts] == ["a.py"]
    detail = conflicts[0].detail
    assert "feature/f1" in detail and "lesson/l1" in detail and "decision/d1" in detail
    assert "\n" not in detail, "a multi-line invariant must not break the block"
    assert "incident/i1" not in detail, "a record asserting nothing contradicts nothing"


def test_records_that_agree_are_not_a_contradiction(repo):
    """The ordinary case — a feature and the lesson written against it — must
    not be reported, or the category stops meaning anything."""
    index = index_of(
        rec("f1", source_paths=("a.py",), invariant="a.py is pure"),
        rec("l1", kind="lesson", source_paths=("a.py",), invariant="a.py is pure"),
    )

    resolution = resolve_context(index, ["f1", "l1"], make_git(repo), max_records=10)

    assert resolution.findings_of(CONTRADICTION) == ()


def test_an_unknown_seed_and_a_duplicated_one_are_different_categories(repo):
    """Having none of these and having two of these are different repairs, so
    they are different reports."""
    index = build_index(
        [
            LoadedRecord(rec("dup"), "one.json"),
            LoadedRecord(rec("dup", kind="lesson"), "two.json"),
            LoadedRecord(rec("f1"), "f1.json"),
        ]
    )

    resolution = resolve_context(index, ["dup", "ghost", "f1"], make_git(repo), max_records=10)

    assert selected_ids(resolution) == ["f1"]
    assert subjects(resolution, UNKNOWN_RECORD) == ["ghost"]
    assert subjects(resolution, DUPLICATE_RECORD_ID) == ["dup", "dup"]
    assert "one.json" in " ".join(f.detail for f in resolution.findings_of(DUPLICATE_RECORD_ID))


def test_a_record_unrelated_to_the_seed_is_not_selected(repo):
    """No similarity search, no embeddings, no "related because the words look
    alike": `stranger` names the same source path, the same kind and nearly the
    same id, and is not reached by an explicit relation."""
    index = index_of(
        rec("f1", source_paths=("a.py",), invariant="a.py is pure", related_ids=("l1",)),
        rec("l1", kind="lesson", source_paths=("a.py",)),
        rec("f1-stranger", source_paths=("a.py",), invariant="a.py is pure"),
    )

    resolution = resolve_context(index, ["f1"], make_git(repo), max_records=10)

    assert selected_ids(resolution) == ["f1", "l1"]


def test_a_relation_is_directed_as_written(repo):
    """Naming B in A's `related_ids` pulls B in when A is selected, and does
    NOT pull A in when B is. A symmetric expansion would drag in every record
    that ever mentioned this one."""
    index = index_of(rec("a", related_ids=("b",)), rec("b", kind="lesson"))

    assert selected_ids(resolve_context(index, ["b"], make_git(repo), max_records=10)) == ["b"]


def test_every_selected_item_carries_a_reason(repo):
    """One line each: a seed reference, or which record related it in."""
    index = index_of(
        rec("f1", related_ids=("l1",)),
        rec("l1", kind="lesson", related_ids=("d1",)),
        rec("d1", kind="decision"),
    )

    resolution = resolve_context(index, ["f1"], make_git(repo), max_records=10)

    for item in resolution.selected:
        assert item.reason and "\n" not in item.reason
    assert {item.record.id: item.depth for item in resolution.selected} == {
        "f1": 0, "l1": 1, "d1": 2,
    }


# ---- 5. the budget -----------------------------------------------------------


def test_a_bound_budget_says_what_it_dropped_and_why(repo):
    """Silent truncation is the failure being prevented, not an acceptable
    degradation. Every dropped record earns a finding naming the budget, its
    own rank and the rule that ranked it."""
    index = index_of(
        rec("f1", related_ids=("l1", "l2")),
        rec("f2", related_ids=("l3",)),
        rec("l1", kind="lesson"),
        rec("l2", kind="lesson"),
        rec("l3", kind="lesson"),
    )

    resolution = resolve_context(index, ["f1", "f2"], make_git(repo), max_records=3)

    assert selected_ids(resolution) == ["f1", "f2", "l1"], "seeds first, then (kind, id)"
    dropped = resolution.findings_of(BUDGET_DROPPED)
    assert [finding.subject for finding in dropped] == ["l2", "l3"]
    assert "max_records=3" in dropped[0].detail
    assert "ranked 4 of 5" in dropped[0].detail
    assert "(depth from seed, kind, id)" in dropped[0].detail


def test_the_budget_never_drops_a_seed_for_a_distant_record(repo):
    """Depth leads the drop rule deliberately: a record the caller named
    explicitly must not lose its place to one three relations away that happens
    to sort earlier by kind."""
    index = index_of(
        rec("zzz", kind="lesson", related_ids=("aaa",)),
        rec("aaa", kind="decision"),
    )

    resolution = resolve_context(index, ["zzz"], make_git(repo), max_records=1)

    assert selected_ids(resolution) == ["zzz"]
    assert subjects(resolution, BUDGET_DROPPED) == ["aaa"]


def test_findings_survive_the_budget_for_the_records_that_survive_it(repo):
    """Verification runs on the KEPT set, so every finding concerns a record the
    caller can actually see — and anything removed is accounted for by its own
    `budget_dropped` line rather than by silence."""
    verified = head(repo)
    commit(repo, "a.py", "a changed\n")
    index = index_of(
        rec("f1", source_paths=("a.py",), last_verified_commit=verified),
        rec("f2", source_paths=("a.py",), last_verified_commit=verified),
    )

    resolution = resolve_context(index, ["f1", "f2"], make_git(repo), max_records=1)

    assert subjects(resolution, STALE_FINDING) == ["f1"]
    assert subjects(resolution, BUDGET_DROPPED) == ["f2"]


@pytest.mark.parametrize("budget", [0, -1, True, 2.5, "10"])
def test_a_budget_the_resolver_would_not_honour_is_refused(repo, budget):
    """Refused rather than clamped, in `[concurrency] lanes`' style: a budget of
    zero returns nothing while reading as configured, and `true` is a count
    wearing a switch's clothes."""
    with pytest.raises(ValueError):
        resolve_context(index_of(rec("f1")), ["f1"], make_git(repo), max_records=budget)


# ---- 6. the rendered block ---------------------------------------------------


def test_every_finding_reaches_the_rendered_block(tmp_path, repo):
    """A problem that lives on the index and never reaches the report is a
    silent drop with extra steps."""
    directory = tmp_path / "records"
    directory.mkdir()
    (directory / "broken.json").write_text("{", encoding="utf-8")
    (directory / "f1.json").write_text(
        json.dumps({"id": "f1", "kind": "feature", "source_paths": ["gone.py"]}),
        encoding="utf-8",
    )

    resolution = resolve_context(load_index(directory), ["f1"], make_git(repo), max_records=10)
    block = render_resolution(resolution)

    assert subjects(resolution, UNREADABLE_RECORD) == ["broken.json"]
    for finding in resolution.findings:
        assert finding.detail in block
    assert "broken.json" in block and "gone.py" in block
    assert len(block.splitlines()) == 1 + len(resolution.selected) + len(resolution.findings)


def test_a_multi_line_title_still_occupies_one_line(repo):
    """The block is line-oriented, and record text is written by people."""
    index = index_of(
        rec("f1", title="first line\nsecond line", last_verified_commit=head(repo))
    )

    block = render_resolution(resolve_context(index, ["f1"], make_git(repo), max_records=10))

    assert len(block.splitlines()) == 2, "one header line, one selected line, no findings"
    assert "first line second line" in block


# ---- 7. the [context] budget setting -----------------------------------------


def write_config(tmp_path: Path, body: str = "") -> Path:
    """A minimal loadable config, plus whatever body is under test. The same
    shape `test_config_concurrency.write_config` uses: `workers_root` is the one
    required key and this section is validated entirely at load time."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nworkers_root = "{tmp_path / "w"}"\n\n' + body, encoding="utf-8"
    )
    return path


def test_an_absent_section_is_the_default_budget(tmp_path):
    """Every config file written before this section existed, which is all of
    them: the template is copied once and never re-read."""
    assert load_config(write_config(tmp_path)).context.max_records == DEFAULT_CONTEXT_MAX_RECORDS
    assert load_config(write_config(tmp_path, "[context]\n")).context == ContextConfig()


def test_the_dataclass_default_carries_to_every_direct_construction():
    """The default that matters for the direct `AutoloopConfig(...)`
    constructions across the suite: none of them names the field."""
    assert AutoloopConfig.__dataclass_fields__["context"].default == ContextConfig()
    assert ContextConfig().max_records == DEFAULT_CONTEXT_MAX_RECORDS


def test_the_field_was_appended_so_positional_construction_is_unmoved():
    """`observed_checkout`'s own comment states the rule this pins: the
    positional meaning of every earlier field must not move. Stated as "after
    everything that predates it" rather than "last", exactly as
    `test_config_concurrency.py` states it, so a later task may append its own
    field without editing this."""
    names = [f.name for f in dataclasses.fields(AutoloopConfig)]
    assert names.index("context") > names.index("concurrency")


@pytest.mark.parametrize("value", ["0", "-1", "true", '"25"', "2.5"])
def test_a_budget_outside_the_supported_range_is_refused_at_load(tmp_path, value):
    """Refused, not clamped: a typo can never read as configured while a
    different number binds."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, f"[context]\nmax_records = {value}\n"))

    assert "context.max_records" in str(exc.value)


def test_an_unknown_key_in_the_section_is_refused(tmp_path):
    """Strict like every other section: a typo'd budget that loaded would be a
    setting nobody is honouring."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, "[context]\nmax_record = 5\n"))

    assert "context" in str(exc.value)


def test_a_neighbouring_section_name_is_still_unknown(tmp_path):
    """The other half of adding a name to `_SECTIONS`: `[contexts]` must stay
    refused rather than joining it."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, "[contexts]\nmax_records = 5\n"))

    assert "unknown config sections" in str(exc.value)


def test_the_section_written_as_a_bare_key_names_itself(tmp_path):
    """`context = 5` gets the loader's own error naming the section, rather
    than `_check_keys` reporting the digits as unknown keys.

    Written ABOVE `[paths]` rather than through `write_config`: a bare key
    after a table header belongs to that table, so the same line appended to
    the body would be a stray key in `[paths]` and would test the wrong
    refusal."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'context = 5\n\n[paths]\nworkers_root = "{tmp_path / "w"}"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    assert "[context] must be a table" in str(exc.value)


def test_the_template_ships_the_section_and_it_loads(tmp_path):
    """A template that documents a key the loader refuses, or ships a value
    that is not the default, hands an operator a config that is not the one it
    reads as. So the shipped section is loaded through `load_config` itself."""
    import tomllib

    example = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    section = example.split("[context]", 1)[1].split("\n[", 1)[0]
    for field in dataclasses.fields(ContextConfig):
        assert f"{field.name} =" in section, f"the template does not document {field.name}"

    shipped_value = tomllib.loads(example)["context"]["max_records"]
    assert shipped_value == DEFAULT_CONTEXT_MAX_RECORDS
    assert isinstance(shipped_value, int) and not isinstance(shipped_value, bool)

    shipped = load_config(write_config(tmp_path, "[context]" + section + "\n"))
    assert shipped == load_config(write_config(tmp_path))
