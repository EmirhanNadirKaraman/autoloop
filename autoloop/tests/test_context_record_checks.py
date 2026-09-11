"""ctx-14: a context record cannot cite a commit, a successor or an index entry
that does not exist — and the checks that prove it run on ctx-03's store.

THE CLAIM, and where each half of it is pinned. §1 is the SHAPE half, at parse
time: a kind carries its required fields, a title is bounded, a commit is
spelled as one commit, and a status is derived rather than asserted — every one
refused by `record_from_mapping`, which is also what `ContextRecordStore.write`
re-reads a record through. §2 is the RESOLUTION half, over `verify_records`
with gateways stated in the test: a commit the repository does not hold, a
successor no file declares, a relation to nothing. §3 is the same through real
git, because "this sha resolves" is a claim about an object database and a fake
would pin this file's opinion of git rather than git.

THE BAR THIS FILE IS GRADED ON IS FAIL-CLOSED, and §2 spends most of its lines
on it: a check that cannot run must REPORT, never pass. So the gateway that
raises, the gateway without the probe, the gateway that answers something other
than a verdict and no gateway at all each appear here, each refusing the record
they could not verify — while `load_records` still returns every record they
did not need to.

Cheap where the claim is pure: §1 and §2 build no repository. §3 builds one, via
`gitrepo.py`, once per test that needs an object database to disagree with.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from gitrepo import make_repo_from_template, run_git

from autoloop.context_index import build_index, load_index
from autoloop.context_records import (
    MAX_SUMMARY_CHARS,
    RECORD_KINDS,
    REQUIRED_FIELDS,
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    STATUSES,
    ContextRecord,
    ContextRecordError,
    ContextRecordStore,
    LoadedRecord,
    load_records,
    load_records_at,
    record_from_mapping,
    repository_record_store,
    verify_records,
)
from autoloop.errors import GitCommandError
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine

SHA_A = "a" * 40
SHA_B = "b" * 40
NOWHERE = "0" * 40

#: One complete mapping per kind — exactly the fields `REQUIRED_FIELDS` names,
#: filled, and nothing else — so a test can blank ONE of them and know that is
#: the only reason the record was refused.
COMPLETE = {
    "decision": {"title": "push by sha", "invariant": "the loop pushes by sha"},
    "feature": {
        "title": "a.py greets once",
        "invariant": "a.py greets exactly once",
        "source_paths": ["a.py"],
    },
    "incident": {"title": "a.py greeted twice", "source_paths": ["a.py"]},
    "lesson": {"title": "one mistake is not a lesson"},
}


def mapping(kind: str, record_id: str = "x", **overrides) -> dict:
    data = {"id": record_id, "kind": kind, **COMPLETE[kind]}
    data.update(overrides)
    return data


def entry(record_id: str, kind: str = "feature", **fields) -> LoadedRecord:
    """A complete record of `kind`, as the loader would hand it to the
    verifier, from a notional `<id>.json`."""
    data = mapping(kind, record_id, **fields)
    return LoadedRecord(record_from_mapping(data), f"{record_id}.json")


def write(directory: Path, name: str, data) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (directory / name).write_text(text, encoding="utf-8")


def messages(problems, source: str) -> str:
    return " | ".join(p.message for p in problems if p.source == source)


def ids(loaded) -> list[str]:
    return [item.record.id for item in loaded]


# ---- gateways stated in the test ---------------------------------------------


class Holding:
    """A gateway whose object database holds exactly `shas`, every one of them
    a commit. Counts probes, for the one-per-distinct-commit claim."""

    def __init__(self, *shas: str):
        self.shas = set(shas)
        self.probed: list[str] = []

    def object_exists(self, oid: str) -> bool:
        self.probed.append(oid)
        return oid in self.shas

    def read_commit(self, oid: str) -> dict:
        if oid not in self.shas:
            raise GitCommandError(f"git cat-file commit {oid} failed (rc=128)")
        return {"tree": f"tree-of-{oid}", "parents": [], "message": ""}


class Dying:
    """A gateway that raises `exc` for every probe — git dying, the worker
    directory gone, whatever the caller says."""

    def __init__(self, exc: Exception):
        self.exc = exc

    def object_exists(self, oid: str) -> bool:
        raise self.exc

    def read_commit(self, oid: str) -> dict:
        raise self.exc


class Mute:
    """A gateway WITHOUT the probe — the shape of every three-method stub in
    this suite, and of an object that is not a `GitGateway` at all."""

    def tree_of(self, rev: str) -> str:
        return f"tree-of-{rev}"


class BlobOnly:
    """A gateway holding the object but unable to read it as a commit — a blob
    or a tree id written where a commit belongs."""

    def object_exists(self, oid: str) -> bool:
        return True

    def read_commit(self, oid: str) -> dict:
        raise GitCommandError(f"git cat-file commit {oid} failed (rc=128): bad file")


class Odd:
    """A gateway that answers the existence question with something that is
    not a verdict."""

    def object_exists(self, oid: str):
        return None

    def read_commit(self, oid: str) -> dict:
        return {"tree": f"tree-of-{oid}", "parents": [], "message": ""}


# =============================================================================
# 1. SHAPE — refused at parse time, so a writer cannot put it on disk either
# =============================================================================


def test_required_fields_cover_exactly_the_record_kinds_and_name_real_fields():
    """The table and `RECORD_KINDS` agree, and every field it names exists on
    the dataclass — a kind without a rule is refused by the parser, and this is
    the test that keeps that branch from ever being the ordinary path."""
    assert set(REQUIRED_FIELDS) == set(RECORD_KINDS)
    fields = {f.name for f in dataclasses.fields(ContextRecord)}
    for kind, required in REQUIRED_FIELDS.items():
        assert required, f"{kind} requires nothing, so nothing about it is checked"
        assert "title" in required, f"a {kind} record with no title is one nobody can review"
        assert set(required) <= fields, (kind, required)


@pytest.mark.parametrize(
    "kind, field",
    [(kind, field) for kind in RECORD_KINDS for field in REQUIRED_FIELDS[kind]],
)
def test_a_kind_missing_a_required_field_is_refused(kind, field):
    """Each kind, each field its claim is made of, blanked one at a time. The
    refusal names the kind, the record and the field, because "invalid record"
    is not a repair anybody can perform."""
    blank = [] if field == "source_paths" else ""
    with pytest.raises(ContextRecordError) as refused:
        record_from_mapping(mapping(kind, "r1", **{field: blank}))
    assert kind in str(refused.value) and field in str(refused.value)
    assert "'r1'" in str(refused.value)


@pytest.mark.parametrize("kind", RECORD_KINDS)
def test_a_record_complete_for_its_kind_parses_with_every_other_field_empty(kind):
    """The rule is exactly the table, not the table plus whatever else happens
    to be empty: a record carrying its kind's fields and nothing more loads."""
    record = record_from_mapping(mapping(kind))
    assert record.kind == kind
    for field in {"title", "invariant", "source_paths"} - set(REQUIRED_FIELDS[kind]):
        assert not getattr(record, field)


def test_a_title_over_the_cap_is_refused_and_one_at_the_cap_is_not():
    at_cap = mapping("lesson", title="t" * MAX_SUMMARY_CHARS)
    assert len(record_from_mapping(at_cap).title) == MAX_SUMMARY_CHARS

    with pytest.raises(ContextRecordError) as refused:
        record_from_mapping(mapping("lesson", title="t" * (MAX_SUMMARY_CHARS + 1)))
    assert str(MAX_SUMMARY_CHARS) in str(refused.value)
    assert str(MAX_SUMMARY_CHARS + 1) in str(refused.value)


@pytest.mark.parametrize(
    "commit",
    ["HEAD", "main", "abc1234", "A" * 40, "a" * 39, "a" * 41, "g" * 40, "--batch", "main^{tree}"],
)
def test_a_commit_not_spelled_as_one_full_object_id_is_refused(commit):
    """A ref, an abbreviation or an uppercase spelling would RESOLVE — and name
    a different commit at a different time, in a different clone, or under a
    comparison the closeout makes verbatim. Only the spelling git prints and
    the closeout writes is one commit. The last two are the security half: the
    value is handed to `git cat-file` by the loader, and a flag or a revision
    expression written into a record file must never reach that argv."""
    with pytest.raises(ContextRecordError) as refused:
        record_from_mapping(mapping("lesson", last_verified_commit=commit))
    assert "last_verified_commit" in str(refused.value)
    assert commit in str(refused.value)


def test_an_empty_commit_and_a_full_one_pass_the_shape_check():
    """Shape only — whether `SHA_A` EXISTS is `verify_records`' question, asked
    with a gateway, and this parse has none to ask. 64 hex is a full id too,
    in a SHA-256 repository, the same pair `tasks._COMMIT_SHA_RE` accepts."""
    assert record_from_mapping(mapping("lesson")).last_verified_commit == ""
    for full in (SHA_A, "c" * 64):
        assert record_from_mapping(mapping("lesson", last_verified_commit=full)).last_verified_commit == full


def test_status_is_derived_from_superseded_by_and_never_asserted():
    """Two states, because `superseded_by` is the only field that can put a
    record in one; a file that asserts a status carries a key nobody defined."""
    assert STATUSES == (STATUS_ACTIVE, STATUS_SUPERSEDED)
    active = record_from_mapping(mapping("decision"))
    retired = record_from_mapping(mapping("decision", superseded_by="next"))
    assert active.status == STATUS_ACTIVE and not active.is_superseded
    assert retired.status == STATUS_SUPERSEDED and retired.is_superseded
    assert {active.status, retired.status} <= set(STATUSES)
    for status in ("active", "resolved", "superseded", "retired"):
        with pytest.raises(ContextRecordError) as refused:
            record_from_mapping(mapping("decision", status=status))
        assert "unknown keys: ['status']" in str(refused.value)


def test_the_kinds_are_the_four_the_merged_tree_uses_and_no_sixth():
    """ctx-02 knew `project` and `architecture`; nothing in the merged tree
    selects, verifies or closes out either, and a kind nothing reads is a claim
    nobody checks. Refused as a kind, before any required-field rule is asked."""
    assert set(RECORD_KINDS) == {"decision", "feature", "incident", "lesson"}
    for kind in ("project", "architecture"):
        with pytest.raises(ContextRecordError) as refused:
            record_from_mapping({"id": "x", "kind": kind, "title": "t"})
        assert "kind must be one of" in str(refused.value)


def test_a_writer_cannot_put_a_record_the_loader_would_refuse_on_disk(tmp_path):
    """`ContextRecordStore.write` re-reads through `record_from_mapping` before
    it touches the disk, so every new shape rule also binds the one writer: a
    record over the cap or short of its kind's fields is never written, and a
    file that loads today is never replaced by one that would not."""
    store = ContextRecordStore(tmp_path / "records", "docs/context")
    good = record_from_mapping(mapping("feature", "feat"))
    assert store.write(good, "feat.json") is not None
    before = (store.directory / "feat.json").read_bytes()

    too_long = dataclasses.replace(good, title="t" * (MAX_SUMMARY_CHARS + 1))
    incomplete = dataclasses.replace(good, invariant="")
    assert store.write(too_long, "feat.json") is None
    assert store.write(incomplete, "feat.json") is None
    assert (store.directory / "feat.json").read_bytes() == before


# =============================================================================
# 2. RESOLUTION — every citation resolves, or the record is a named problem
# =============================================================================


def test_a_commit_the_repository_does_not_hold_refuses_the_record():
    """The check ctx-03 did not have: `_check_source_path` proved a path was
    path-SHAPED, and nothing proved a commit was anything but sha-shaped."""
    held = entry("held", last_verified_commit=SHA_A)
    missing = entry("missing", last_verified_commit=SHA_B)
    unverified = entry("unverified")  # cites no commit, so nothing to resolve

    accepted, problems = verify_records((held, missing, unverified), Holding(SHA_A))

    assert ids(accepted) == ["held", "unverified"]
    assert [p.source for p in problems] == ["missing.json"]
    assert SHA_B in problems[0].message and "does not exist" in problems[0].message


@pytest.mark.parametrize(
    "gateway, expect",
    [
        (Dying(GitCommandError("git cat-file -e failed (rc=128): not a git repository")), "GitCommandError"),
        (Dying(OSError("worker directory vanished")), "OSError"),
        (Mute(), "AttributeError"),
        (Odd(), "instead of True/False"),
    ],
)
def test_a_gateway_that_cannot_answer_reports_the_record_rather_than_accepting_it(
    tmp_path, gateway, expect
):
    """THE FAIL-CLOSED PAIR, through `load_records`. The gateway raises (git
    dying, the directory gone), has no probe at all, or answers with something
    that is not a verdict — and in every case the record that needed it is
    REPORTED and not returned, while the record that cited nothing and the file
    that never parsed come back exactly as they would have with a working
    gateway. Nothing raises past the loader."""
    directory = tmp_path / "records"
    write(directory, "cites.json", mapping("feature", "cites", last_verified_commit=SHA_A))
    write(directory, "plain.json", mapping("feature", "plain"))
    write(directory, "broken.json", "{not json")

    loaded, problems = load_records(directory, gateway)

    assert ids(loaded) == ["plain"], "the records it could read are still returned"
    assert [p.source for p in problems] == ["broken.json", "cites.json"]
    assert "could not be verified" in messages(problems, "cites.json")
    assert expect in messages(problems, "cites.json")
    assert SHA_A in messages(problems, "cites.json")
    assert "not valid JSON" in messages(problems, "broken.json")


def test_no_gateway_is_a_starved_check_and_not_a_pass(tmp_path):
    """`load_records(directory)` — the one-argument call every caller had — is
    still answered, and a record naming a commit is a problem in that answer,
    because nothing was there to resolve it. A record naming none loads."""
    directory = tmp_path / "records"
    write(directory, "cites.json", mapping("lesson", "cites", last_verified_commit=SHA_A))
    write(directory, "plain.json", mapping("lesson", "plain"))

    loaded, problems = load_records(directory)

    assert ids(loaded) == ["plain"]
    assert [p.source for p in problems] == ["cites.json"]
    assert "no repository was given" in problems[0].message

    # And the loop-private store hands whatever it is given straight through:
    # a gateway resolves the citation, `None` starves the check the same way.
    store = ContextRecordStore(directory, "docs/context")
    assert ids(store.load(Holding(SHA_A), "")[0]) == ["cites", "plain"]
    assert ids(store.load()[0]) == ["plain"]


def test_an_object_that_is_not_a_commit_is_refused():
    """`object_exists` says yes to a blob and to a tree; `cat-file commit` says
    no to both, and the resolver would otherwise call such a record's staleness
    unknown forever. Refused here instead, naming the reason."""
    accepted, problems = verify_records((entry("blob", last_verified_commit=SHA_A),), BlobOnly())

    assert accepted == ()
    assert "is not a commit" in problems[0].message and SHA_A in problems[0].message


def test_one_probe_per_distinct_commit_and_none_for_a_load_that_cites_nothing():
    """Forty records verified at one commit cost one probe; a directory whose
    records cite no commit costs none. Counted on the gateway rather than
    trusted from the docstring."""
    gateway = Holding(SHA_A, SHA_B)
    records = tuple(entry(f"r{n}", last_verified_commit=SHA_A) for n in range(5)) + (
        entry("other", last_verified_commit=SHA_B),
        entry("none"),
    )

    accepted, problems = verify_records(records, gateway)

    assert len(accepted) == 7 and problems == ()
    assert sorted(gateway.probed) == [SHA_A, SHA_B]

    quiet = Holding()
    assert verify_records((entry("none"),), quiet) == ((entry("none"),), ())
    assert quiet.probed == []


def test_a_superseded_record_whose_successor_is_absent_is_refused():
    """ctx-02's `_check_successors` on ctx-03's `superseded_by`: the write
    (`superseded_record`) cannot see the directory, so the read-back is where
    "the successor is actually present" is asked. Present is accepted; absent
    and self-succession are refused, each naming the successor."""
    old = entry("old", kind="decision", superseded_by="new")
    new = entry("new", kind="decision")
    gone = entry("gone", kind="decision", superseded_by="never-written")
    selfish = entry("selfish", kind="decision", superseded_by="selfish")

    accepted, problems = verify_records((old, new, gone, selfish), Holding())

    assert ids(accepted) == ["old", "new"]
    assert [p.source for p in problems] == ["gone.json", "selfish.json"]
    assert "'never-written'" in messages(problems, "gone.json")
    assert "names the record itself" in messages(problems, "selfish.json")


def test_a_successor_whose_file_did_not_parse_counts_as_absent(tmp_path):
    """A successor nobody can read is a successor nobody can follow. The old
    record is refused with the successor's name, and the successor's file is
    refused with its own reason — two problems, two repairs, neither hidden."""
    directory = tmp_path / "records"
    write(directory, "old.json", mapping("decision", "old", superseded_by="new"))
    write(directory, "new.json", "{not json")

    loaded, problems = load_records(directory)

    assert loaded == ()
    assert [p.source for p in problems] == ["new.json", "old.json"]
    assert "'new'" in messages(problems, "old.json")


def test_a_relation_to_an_id_no_file_declares_is_refused():
    """The third clause of the claim — an INDEX ENTRY that does not exist. A
    `related_ids` entry is a citation of one, and the resolver reports it only
    when a selection happens to follow the edge; the loader refuses it for
    every load, whether or not anything selects the record."""
    cites = entry("cites", related_ids=("real", "ghost"))
    real = entry("real")

    accepted, problems = verify_records((cites, real), Holding())

    assert ids(accepted) == ["real"]
    assert [p.source for p in problems] == ["cites.json"]
    assert "'ghost'" in problems[0].message and "'real'" not in problems[0].message


def test_a_refused_record_does_not_refuse_the_records_that_cite_it():
    """No cascade. Citations are checked against the PARSED set: `b` is
    refused for its own commit, and `a`, which relates to `b`, still loads —
    one bad citation is one problem at its own source, not a component of the
    graph going dark. The resolver reports the edge into `b` when it follows
    it, which is the finding it already had."""
    a = entry("a", related_ids=("b",))
    b = entry("b", last_verified_commit=NOWHERE)
    c = entry("c", kind="decision", superseded_by="b")

    accepted, problems = verify_records((a, b, c), Holding())

    assert ids(accepted) == ["a", "c"]
    assert [p.source for p in problems] == ["b.json"]


def test_verification_never_resolves_a_duplicated_id_by_which_copy_passed():
    """Two files declare `x`; one cites a commit that does not exist. Dropping
    that copy alone would leave the other as THE `x` — a duplicate settled by
    which file verified, the "one of them wins" `context_index` refuses. Both
    copies reach the index, which indexes `x` under neither and names both
    files; the failing copy's problem is reported as well, and is true: it is
    in no index either way."""
    fine = LoadedRecord(record_from_mapping(mapping("feature", "x")), "one.json")
    failing = LoadedRecord(
        record_from_mapping(mapping("feature", "x", last_verified_commit=NOWHERE)), "two.json"
    )

    accepted, problems = verify_records((fine, failing), Holding())

    assert accepted == (fine, failing)
    assert [p.source for p in problems] == ["two.json"]
    index = build_index(accepted, problems)
    assert index.get("x") is None
    assert index.duplicate_ids["x"] == ("one.json", "two.json")
    assert [p.source for p in index.problems] == ["two.json"]


class TreeOnly:
    """A gateway that can read a tree — enough for `load_records_at` to list
    and read every record — and has no `object_exists`: the revision loader
    verifies through the same gateway it read with, and this is the one that
    cannot answer the second question."""

    def __init__(self, files: dict[str, dict]):
        self.blobs = {f"oid-{name}": json.dumps(data).encode() for name, data in files.items()}
        self.names = list(files)

    def tree_of(self, rev: str) -> str:
        return f"tree-of-{rev}"

    def tree_entries(self, tree: str) -> dict:
        return {f"docs/context/{name}": ("100644", "blob", f"oid-{name}") for name in self.names}

    def blob_bytes(self, oid: str) -> bytes:
        return self.blobs[oid]


def test_the_revision_loader_is_starved_the_same_way_as_the_disk_loader():
    """`load_records_at` reads the tree through a gateway and then verifies
    through the SAME gateway. One that lists and reads but has no probe reports
    every record citing a commit — the read succeeding is not the check
    passing — and returns the record that cited nothing."""
    git = TreeOnly(
        {
            "cites.json": mapping("lesson", "cites", last_verified_commit=SHA_A),
            "plain.json": mapping("lesson", "plain"),
        }
    )

    loaded, problems = load_records_at(git, SHA_B, "docs/context")

    assert ids(loaded) == ["plain"]
    assert [p.source for p in problems] == ["cites.json"]
    assert "AttributeError" in problems[0].message and SHA_A in problems[0].message


def test_every_failed_check_on_one_record_is_its_own_problem():
    """One record, three bad citations, three lines — each a different repair,
    and a reader told only the first would fix it and be refused again."""
    record = entry(
        "bad", kind="decision", last_verified_commit=NOWHERE, superseded_by="nope", related_ids=("nah",)
    )

    accepted, problems = verify_records((record,), Holding())

    assert accepted == ()
    assert [p.source for p in problems] == ["bad.json"] * 3
    joined = messages(problems, "bad.json")
    assert NOWHERE in joined and "'nope'" in joined and "'nah'" in joined


# =============================================================================
# 3. THROUGH REAL GIT — the object database is the one that disagrees
# =============================================================================


def gateway(root) -> GitGateway:
    return GitGateway(Path(root), PolicyEngine(PolicyConfig()))


def test_a_real_commit_resolves_and_a_fake_one_does_not(tmp_path):
    """`cat-file -e` and `cat-file commit` against a real repository: the
    commit that exists loads, the sha that does not is refused as absent, and
    the BLOB id of a file that exists and the TREE id of the commit itself are
    each refused as not a commit — `object_exists` alone would have passed
    both, and `rev-parse ^{tree}` would have passed the tree."""
    repo = make_repo_from_template(tmp_path / "repo", files=(("a.py", "a\n"),))
    git = gateway(repo)
    head = run_git(repo, "rev-parse", "HEAD").strip()
    tree = git.tree_of(head)
    blob = git.tree_entries(tree)["a.py"][2]
    directory = tmp_path / "records"
    write(directory, "real.json", mapping("feature", "real", last_verified_commit=head))
    write(directory, "fake.json", mapping("feature", "fake", last_verified_commit=NOWHERE))
    write(directory, "blob.json", mapping("feature", "blob", last_verified_commit=blob))
    write(directory, "tree.json", mapping("feature", "tree", last_verified_commit=tree))

    loaded, problems = load_records(directory, git)

    assert ids(loaded) == ["real"]
    assert [p.source for p in problems] == ["blob.json", "fake.json", "tree.json"]
    assert "does not exist" in messages(problems, "fake.json")
    assert "is not a commit" in messages(problems, "blob.json")
    assert "is not a commit" in messages(problems, "tree.json")


def test_an_index_cannot_hold_a_record_whose_citation_disk_does_not_declare(tmp_path):
    """The index-versus-disk reconciliation, as ctx-03's derived index can
    state it: `load_index` over a directory carries every citation the
    directory cannot satisfy as a problem, and holds no entry for the record
    that made it. So nothing in `by_id` cites an id no file declares, and the
    resolver's "in no index and can be selected by nothing" is true of every
    problem it renders. Through the repository store as well, at a revision,
    because that is the store production reads."""
    repo = make_repo_from_template(tmp_path / "repo", files=(("a.py", "a\n"),))
    git = gateway(repo)
    head = run_git(repo, "rev-parse", "HEAD").strip()
    records = repo / "docs" / "context"
    write(records, "kept.json", mapping("feature", "kept", last_verified_commit=head))
    write(records, "dangling.json", mapping("decision", "dangling", superseded_by="absent"))
    write(records, "unresolved.json", mapping("lesson", "unresolved", last_verified_commit=NOWHERE))

    index = load_index(records, git)

    assert [record.id for record in index.records] == ["kept"]
    assert index.get("dangling") is None and index.get("unresolved") is None
    assert [p.source for p in index.problems] == ["dangling.json", "unresolved.json"]

    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "records")
    at = run_git(repo, "rev-parse", "HEAD").strip()
    store = repository_record_store(repo, "docs/context")
    loaded, problems = store.load(git, at)
    assert ids(loaded) == ["kept"]
    assert [p.source for p in problems] == ["dangling.json", "unresolved.json"]
    # The same answer built the way the loop builds it, so the problems the
    # packet will count are the refusals and not a second reading of them.
    assert build_index(loaded, problems).problems == index.problems
