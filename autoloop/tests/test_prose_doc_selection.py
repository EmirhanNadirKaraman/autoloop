"""A prose docs change must not select the whole suite (select-02, 2026-08-27).

`select_validation_commands` narrows a validated round to the tests its changed
paths reach. select-01 (2026-08-26) stopped one unresolvable path from vetoing
that answer, and val-04 (2026-08-27) wired the selector into the PRE-commit run
as well — and the feature still narrowed nothing on a real round, because
`CLAUDE.md` requires every task to append a change note to the documentation
trackers, so a tracker `.md` is in essentially every commit the loop makes.

A `.md` resolves to no module, so it fell to the reference-token rule, whose
tokens for `docs/TESTS.md` are `('.md', 'TESTS', 'TESTS.md', 'docs',
'docs/TESTS.md')`. The bare `docs` and `.md` match any source that CITES a
document in a docstring, and this repository's modules document themselves by
naming their docs. Measured HERE, on this checkout, 2026-08-27: every one of
the six trackers was attributed 93 of 93 test files — the whole suite, on the
note every task is required to write. (The brief's own figures — 198 of 512
graph files, 81/95 against 95/95 — are from the PRE-SPLIT repository, which
had a second `tests/` tree and a `docs/AUTOLOOP.md`. They are not repeated as
if they had been re-measured here; see
`test_the_change_note_round_no_longer_selects_the_whole_suite` for what was.)

What this file pins is the carve-out that replaced it for `.md` paths ONLY, and
the four things the task required to keep holding with it:

1. `test_docs_merge.py` still runs on a DOCS-ONLY round — the one test a change
   note most needs, since it is what fails an over-long note before the note
   reaches a merge. Asserted on a round with no Python in it at all, so
   attribution is the only thing that could have selected it.
2. `docs/audit_charters.toml` is not prose. It is a runtime input the audit
   parses, and it keeps the reference-token treatment EXACTLY — asserted as an
   equality against that rule recomputed here, not as "it still selects
   something".
3. Nothing else loosened: the closure still runs, an unreadable file is still a
   seed for every document, and a document nothing reads still widens the whole
   run.
4. Nothing to drift. There is no allowlist of doc-reading tests to maintain —
   the set is derived from the checkout on every call, and
   `test_a_new_doc_reading_test_is_selected_without_any_list_being_updated`
   demonstrates that a test file added after this was written is picked up with
   no list edited.

   The derivation has TWO gaps and both are silent — each leaves the attributed
   set INCOMPLETE rather than empty, so the widening backstop never fires. A
   reader that BUILDS a document's name instead of spelling it is asserted
   absent from the checkout by
   `test_no_file_that_addresses_this_checkout_builds_a_document_name_dynamically`.
   A reader that names the document but takes the checkout root from another
   module cannot arise here: `conftest.py` exports no root constant — it does
   `sys.path.insert` and nothing else, which its own docstring makes a
   requirement — and nothing else in the package publishes one. Neither is
   argued away; the second is a claim about this checkout, and it is the one to
   re-check first if a root constant is ever introduced.

Fixture-first, with the real-repository claims at the bottom. The fixtures are
what say WHY each shape is or is not a dependency; the real-repository tests are
what say the rule survives contact with this checkout.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from autoloop.tasks import TRACKER_PATHS
from autoloop.validation import (
    _PROSE_DOC_SUFFIX,
    _addresses_own_checkout,
    _code_strings,
    _files_reading_documents,
    _files_referencing,
    _glob_constrains,
    _is_prose_document,
    _is_test_file,
    _names_document,
    _reference_tokens,
    build_import_graph,
    select_validation_commands,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

RUFF = ("ruff", "check", ".")
AUTOLOOP_SUITE = ("python3", "-m", "pytest", "autoloop/tests", "-q", "-n", "auto")
REAL_COMMANDS = (RUFF, AUTOLOOP_SUITE)

FIXTURE_SUITE = ("python3", "-m", "pytest", "suite", "-q")
COMMANDS = (RUFF, FIXTURE_SUITE)

#: The documents every task is required to append a change note to. Imported
#: rather than written out: the claim below is about the paths the loop
#: actually authorizes, and a second copy here would agree today and disagree
#: the first time that tuple moved.
TRACKERS = TRACKER_PATHS

#: `build_import_graph(REPO_ROOT)`, a real-repository selection and the
#: reader map over the trackers each walk the whole checkout. Cached per process
#: for the same reason `test_test_selection.py` caches its own: several tests
#: below ask about the same tree, the same rounds and the same readers, and the
#: tree does not change while the suite runs.
_REAL_GRAPH = None
_ROUNDS: dict[tuple[str, ...], object] = {}
_READERS: dict[str, frozenset[str]] | None = None


def real_graph():
    global _REAL_GRAPH
    if _REAL_GRAPH is None:
        _REAL_GRAPH = build_import_graph(REPO_ROOT)
    return _REAL_GRAPH


def real_round(*paths: str):
    key = tuple(sorted(paths))
    if key not in _ROUNDS:
        _ROUNDS[key] = select_validation_commands(REAL_COMMANDS, list(key), REPO_ROOT)
    return _ROUNDS[key]


def real_readers():
    """Which repository files READ each tracker, over the real checkout.

    One call for the whole file. It is the same question every caller here asks
    — `TRACKERS` against this checkout — and it costs a walk of every file in
    the graph, so asking it once per test made the readers map, not the claim
    under test, the expensive part of three of them.
    """
    global _READERS
    if _READERS is None:
        _READERS = _files_reading_documents(REPO_ROOT, sorted(real_graph().files), TRACKERS)
    return _READERS


def token_rule_tests(path: str) -> frozenset[str]:
    """The test files `path` would be attributed under the UNCHANGED rule.

    Recomputed from the two functions select-02 did not touch, so a test can
    assert "this path's treatment did not change" as an EQUALITY rather than as
    a count that would still pass if the set had been swapped for another of
    the same size.
    """
    graph = real_graph()
    seeds = _files_referencing(
        REPO_ROOT, sorted(graph.files), {path: _reference_tokens(path)}
    )[path]
    if not seeds:
        return frozenset()
    return frozenset(rel for rel in graph.reachable_from(seeds) if _is_test_file(rel))


def write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def doc_repo(root: Path) -> Path:
    """One prose document, and one module of every shape that could be called a
    reference to it.

    * `pkg/reader.py` READS it, out of the checkout it is part of. This is the
      only shape that can see a change to the document's bytes.
    * `pkg/holder.py` HOLDS the path exactly, and cannot read it: no `__file__`,
      so it has no checkout to resolve the path against. The
      `tasks.TRACKER_PATHS` shape — six exact tracker literals in one of the
      most widely imported modules in the package.
    * `pkg/citer.py` CITES it, in a module docstring, in a comment, and inside a
      sentence that happens to live in a string. The `cli.py` / `config.py`
      shape, and the one that used to attribute the whole checkout.

    Each has a test importing it, so what is and is not selected is visible in
    `selected` rather than in an internal set.
    """
    write(root, "docs/NOTES.md", "notes\n")
    write(root, "pkg/__init__.py", "")
    write(
        root,
        "pkg/reader.py",
        "from pathlib import Path\n\n"
        'TEXT = (Path(__file__).resolve().parents[1] / "docs/NOTES.md").read_text()\n',
    )
    write(root, "pkg/holder.py", 'TRACKERS = ("docs/NOTES.md",)\n')
    write(
        root,
        "pkg/citer.py",
        '"""The note format is described in docs/NOTES.md."""\n\n'
        "from pathlib import Path\n\n"
        "# docs/NOTES.md again, in a comment this time\n"
        "HERE = Path(__file__).resolve().parents[1]\n"
        'MESSAGE = "see docs/NOTES.md for the format"\n',
    )
    write(root, "suite/conftest.py", "import pytest\n")
    write(
        root,
        "suite/test_reader.py",
        "from pkg.reader import TEXT\n\n\ndef test_text():\n    assert TEXT\n",
    )
    write(
        root,
        "suite/test_holder.py",
        "from pkg.holder import TRACKERS\n\n\ndef test_holds():\n    assert TRACKERS\n",
    )
    write(
        root,
        "suite/test_citer.py",
        "from pkg.citer import MESSAGE\n\n\ndef test_cites():\n    assert MESSAGE\n",
    )
    write(root, "suite/test_unrelated.py", "def test_arithmetic():\n    assert 1 + 1 == 2\n")
    return root


# ---- what counts as reading a document --------------------------------------


def test_the_carve_out_is_keyed_on_the_extension_and_not_on_the_directory():
    """`.md` IS the test and `docs/` is NOT, which is the requirement that
    keeps `docs/audit_charters.toml` on the old rule.

    Extension alone tells the two apart here, so no hand-written list of prose
    paths is needed — and a list is the thing that would have had to be kept in
    step with `docs/` the first time a machine-read file was added beside the
    trackers.
    """
    for prose in ("docs/TESTS.md", "docs/SUMMARY.md", "CLAUDE.md", "README.md"):
        assert _is_prose_document(prose), prose
    for machine_read in (
        "docs/audit_charters.toml",
        "docs/codex-app-server-protocol.generated.ts",
        "autoloop/validation.py",
        "pytest.ini",
        "autoloop/seed_tasks.json",
    ):
        assert not _is_prose_document(machine_read), machine_read


def test_a_docstring_or_a_comment_is_not_an_evaluated_string():
    """The mechanism the whole change turns on. A comment never reaches the AST
    at all; a docstring is an `Expr` statement and is dropped here."""
    tree = ast.parse(
        '"""Module prose naming docs/MODULE.md."""\n\n'
        "# docs/COMMENT.md\n"
        'PATH = "docs/CODE.md"\n\n\n'
        "def f():\n"
        '    """Function prose naming docs/FUNCTION.md."""\n'
        "    return PATH\n"
    )

    assert _code_strings(tree) == {"docs/CODE.md"}


def test_only_a_file_that_resolves_its_own_checkout_can_open_a_document_in_it():
    """`__file__` read off the AST, so a file merely TALKING about `__file__`
    does not qualify — the same prose/code split `_code_strings` makes."""
    assert _addresses_own_checkout(
        ast.parse("from pathlib import Path\n\nROOT = Path(__file__).parent\n")
    )
    assert not _addresses_own_checkout(
        ast.parse('"""This module talks about __file__."""\n\nNAME = "__file__"\n')
    )


def test_a_sentence_that_embeds_a_document_name_is_not_a_reference_to_it():
    """Equality, not containment, and this is the case that forces it.

    `config.py` and `cli.py` both raise errors reading "see docs/SECURITY.md
    S31" — evaluated strings, in two of the most widely imported modules in the
    package, and both files carry `__file__`. Under containment a
    `docs/SECURITY.md` round would seed them and the closure would take the
    whole suite, on one of the four trackers every task writes.
    """
    strings = {
        "see docs/SECURITY.md S31.",
        "than answering it — see docs/SECURITY.md S24.",
        "docs/SUMMARY.md",
        "TESTS.md",
    }

    assert not _names_document(strings, "docs/SECURITY.md", "SECURITY.md")
    assert _names_document(strings, "docs/SUMMARY.md", "SUMMARY.md")
    assert _names_document(strings, "docs/TESTS.md", "TESTS.md"), "the basename alone"


def test_a_directory_sweep_reads_every_document_it_matches(tmp_path):
    """Dropping the extension token is what stops `.md` attributing the
    checkout, and a sweep is what that would otherwise have cost: a file doing
    `rglob("*.md")` reads a document it never spells out. Matched as a glob
    instead, which no tracker path can trip.
    """
    root = tmp_path / "sweep"
    write(root, "docs/NOTES.md", "notes\n")
    write(root, "pkg/__init__.py", "")
    write(
        root,
        "pkg/sweeper.py",
        "from pathlib import Path\n\n"
        'FOUND = sorted(Path(__file__).resolve().parents[1].rglob("*.md"))\n',
    )
    write(
        root,
        "suite/test_sweeper.py",
        "from pkg.sweeper import FOUND\n\n\ndef test_found():\n    assert FOUND\n",
    )
    write(root, "suite/test_unrelated.py", "def test_arithmetic():\n    assert 1 + 1 == 2\n")

    chosen = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert not chosen.widened, chosen.reason
    assert chosen.selected == ("suite/test_sweeper.py",)


# ---- the shapes, end to end -------------------------------------------------


def test_only_the_test_of_the_module_that_reads_the_document_is_selected(tmp_path):
    """The whole claim, on one fixture holding all three shapes at once.

    `suite/test_reader.py` is selected THROUGH the closure — it never names the
    document itself — and the two tests whose modules merely hold or cite the
    path are not. Before select-02 all three were selected, because
    `pkg/holder.py` and `pkg/citer.py` both contain the string `docs`.
    """
    root = doc_repo(tmp_path / "shapes")

    chosen = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert not chosen.widened, chosen.reason
    assert chosen.resolved == (), "no Python changed: only attribution can select"
    assert chosen.selected == ("suite/test_reader.py",)
    assert chosen.attributed == (("docs/NOTES.md", 1),)


def test_the_closure_still_runs_from_whatever_seeds_attribution_finds(tmp_path):
    """select-02 narrowed the SEEDS and left the closure alone, and this is the
    test that says so: `suite/test_reader.py` names no document, no `docs` and
    no `.md`. Its only route to the change is the module it imports.
    """
    root = doc_repo(tmp_path / "closure")
    source = (root / "suite/test_reader.py").read_text(encoding="utf-8")

    assert "docs" not in source and ".md" not in source

    chosen = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert "suite/test_reader.py" in chosen.selected


def test_a_document_nothing_reads_still_widens_the_whole_run(tmp_path):
    """The backstop, unchanged. Attribution is tested on the SEEDS, so a
    document with no reader is attributed nothing and the run goes full-suite
    naming that path — never "no reader found, therefore nothing to run".
    """
    root = tmp_path / "unread"
    write(root, "docs/NOTES.md", "notes\n")
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/holder.py", 'TRACKERS = ("docs/NOTES.md",)\n')
    write(
        root,
        "suite/test_holder.py",
        "from pkg.holder import TRACKERS\n\n\ndef test_holds():\n    assert TRACKERS\n",
    )

    chosen = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert chosen.widened
    assert chosen.commands == COMMANDS
    assert chosen.unattributed == ("docs/NOTES.md",)
    assert "docs/NOTES.md" in chosen.reason


def test_a_file_that_cannot_be_read_or_parsed_is_a_seed_for_every_document(tmp_path):
    """The fail-open this scan could have been, closed the same way
    `_files_referencing` closes it.

    A scan that quietly drops its unreadable input is a check that silently
    passes. Leaning on `build_import_graph` having marked the same file
    `opaque` would not close it either — a file that parsed during the walk and
    became unreadable a moment later is in neither set — so correctness would
    rest on a race between two walks of the same tree. `pkg/vanished.py` is
    never written; `pkg/unparseable.py` is written and does not parse.
    """
    root = doc_repo(tmp_path / "broken")
    write(root, "pkg/unparseable.py", "def f(:\n")

    hits = _files_reading_documents(
        root,
        ["pkg/reader.py", "pkg/citer.py", "pkg/unparseable.py", "pkg/vanished.py"],
        ["docs/NOTES.md", "docs/NOBODY_READS_THIS.md"],
    )

    assert hits["docs/NOTES.md"] == frozenset(
        {"pkg/reader.py", "pkg/unparseable.py", "pkg/vanished.py"}
    )
    assert hits["docs/NOBODY_READS_THIS.md"] == frozenset(
        {"pkg/unparseable.py", "pkg/vanished.py"}
    ), "a document nothing names still gets the files that could not be examined"


def test_a_new_doc_reading_test_is_selected_without_any_list_being_updated(tmp_path):
    """Requirement 4, answered by construction rather than by a pin.

    There is no allowlist of doc-reading tests: the set is derived from the
    checkout on every call. A test file that did not exist when this rule was
    written is picked up by the same call, with nothing edited anywhere — which
    is the failure mode a hand-maintained list has and this does not.
    """
    root = doc_repo(tmp_path / "grown")
    before = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert "suite/test_fresh.py" not in before.selected

    write(
        root,
        "suite/test_fresh.py",
        "from pathlib import Path\n\n\n"
        "def test_notes():\n"
        '    assert (Path(__file__).resolve().parents[1] / "docs/NOTES.md").is_file()\n',
    )
    after = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert "suite/test_fresh.py" in after.selected
    assert "suite/test_holder.py" not in after.selected, "and nothing else moved"


def test_a_non_prose_path_under_docs_keeps_the_reference_token_treatment(tmp_path):
    """The `.toml` half of requirement 2, on a fixture where the difference is
    visible: one module cites `config/settings.toml` in a docstring only. Prose
    would exclude it; the token rule includes it, and does here.
    """
    root = tmp_path / "toml"
    write(root, "config/settings.toml", "x = 1\n")
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/citer.py", '"""Reads config/settings.toml."""\n\nVALUE = 1\n')
    write(
        root,
        "suite/test_citer.py",
        "from pkg.citer import VALUE\n\n\ndef test_value():\n    assert VALUE\n",
    )
    write(root, "suite/test_unrelated.py", "def test_arithmetic():\n    assert 1 + 1 == 2\n")

    chosen = select_validation_commands(COMMANDS, ["config/settings.toml"], root)

    assert not chosen.widened, chosen.reason
    assert chosen.selected == ("suite/test_citer.py",)


# ---- the same rule against the REAL repository ------------------------------
#
# `tests/` and `docs/AUTOLOOP.md` exist in the repository the task's numbers
# were measured in and NOT in this extracted checkout, so the second column of
# that table cannot be reproduced here and is not invented. What these assert is
# the shape of the claim over the tree that is actually validated.

DOCS_ONLY = tuple(sorted(TRACKERS))

#: What a change-note-only round selects, out of the whole suite, measured on
#: this checkout after select-04 (2026-09-01) took the files that spawn nothing
#: off the opaque frontier. Stated as a NUMBER rather than as "fewer": the
#: before/after pair, and what the remainder is made of, live in
#: `test_test_selection.py`, which is where the opacity rule itself is pinned.
DOCS_ONLY_SELECTED = 20
#: 102 -> 103 when wanted-01 added `test_wanted_decision.py` (2026-09-01). Only
#: the DENOMINATOR moved: the new file reads no tracker and spawns nothing, so a
#: docs-only round still selects the same 20.
#: 103 -> 104 when prov-01 added `test_codex_stdout_verdict.py` (2026-09-01),
#: for the same reason and with the same effect: still the same 20.
#: 104 -> 105 when prov-02 added `test_codex_preflight.py` (2026-09-01): it
#: fakes the invocation boundary and reads no tracker, so still the same 20.
#: 105 -> 106 when conc-02 added `test_config_concurrency.py` (2026-09-01):
#: it loads configs from `tmp_path` strings and reads no tracker, so the same
#: 20 once more — the DENOMINATOR only.
#: 106 -> 107 when conc-05 added `test_lane_state.py` (2026-09-01): it works
#: on lane paths and lease records under `tmp_path` and reads no tracker, so
#: the same 20 once more — the DENOMINATOR only.
#: 107 -> 108 when conc-06 added `test_fleet_supervisor.py` (2026-09-02): it
#: plans against registries built in memory, names its trackers through
#: `tasks.TRACKER_PATHS` rather than by spelling them, and resolves no
#: `__file__` — so it reads no document under either half of the rule above and
#: the same 20 hold. The DENOMINATOR only.
#: 108 -> 109 when conc-11 added `test_fleet_throttle.py` (2026-09-02): it works
#: on one small JSON record and two orchestrators under `tmp_path`, names its
#: trackers not at all, resolves no `__file__`, and its only concurrency is
#: `threading` — which is not a spawn under the opacity rule — so the same 20
#: once more. The DENOMINATOR only.
#: 109 -> 110 when conc-03b added `test_merge_rereview.py` (2026-09-03): it
#: names no tracker, resolves no `__file__`, and its `run_git` hands an argv
#: this cannot read to `subprocess.run` WITHOUT any interpreter literal in the
#: file — `opaque` is `starts_an_interpreter or (interpreter_seen and
#: unreadable)`, and both terms are False — so the same 20 once more. The
#: DENOMINATOR only.
SUITE_SIZE = 110


def test_a_docs_only_round_selects_a_measured_fraction_of_the_suite():
    """The number this file's whole claim is about, on the round every task in
    this repository makes.

    Two things could make it drift and only one of them is a regression: the
    suite growing (which moves the denominator, and is why it is asserted too)
    and the attribution or the opaque frontier widening (which moves this one).
    Asserted here rather than only in `test_test_selection.py` because a
    docs-only round is what THIS file exists for, and because it is the round
    where nothing but attribution and the frontier can select anything at all.
    """
    graph = real_graph()
    chosen = real_round(*DOCS_ONLY)

    assert not chosen.widened, chosen.reason
    assert chosen.resolved == (), "no Python changed"
    measured = (len(chosen.selected), len(graph.test_files))
    assert measured == (DOCS_ONLY_SELECTED, SUITE_SIZE), (
        f"(selected, suite) = {measured}; selected {sorted(chosen.selected)}"
    )


def test_a_docs_only_round_still_selects_test_docs_merge():
    """REQUIREMENT 1, and the reason it is asserted on a docs-only round.

    `test_docs_merge.py` enforces change-note line length and append-only
    tracker shape. It is the single test a change note most needs, and one
    over-long note fails validation and throws the round away (two full rounds
    lost that way on 2026-08-21). Selecting fewer tests must never mean
    selecting this one less.

    Nothing Python changes in this round, so `resolved` is empty and the import
    closure has no starting point of its own: attribution is the only thing that
    could have selected anything. And `test_docs_merge.py` is not on the opaque
    frontier either — it spawns no interpreter and imports nothing dynamically —
    so its presence cannot be the frontier answering instead of the rule. Both
    are asserted, because either one alone would make this pass without the
    attribution having done anything.
    """
    graph = real_graph()
    target = "autoloop/tests/test_docs_merge.py"

    assert target not in graph.opaque, "otherwise it is selected for every change"

    chosen = real_round(*DOCS_ONLY)

    assert not chosen.widened, chosen.reason
    assert chosen.resolved == ()
    assert target in chosen.selected


@pytest.mark.parametrize("tracker", TRACKERS)
def test_every_tracker_has_a_reader_so_a_change_note_round_never_widens(tracker):
    """The property that keeps requirement 1 true for each tracker separately.

    A document with no reader is attributed nothing and widens the whole run —
    correct, but it would mean the narrowing never fires for that tracker. Read
    off `tasks.TRACKER_PATHS` rather than a copy, so a tracker added to the
    loop's authorization list without a test that reads it fails here — as a new
    CASE, since the parametrisation reads off the same tuple.

    Parametrised rather than looped over inside one test (2026-08-28). Each
    tracker costs a full real-repository selection, and six in series made this
    the slowest test in the suite: 104s under `-n auto`, all of it on one worker
    while the others had nothing left to do. Six cases spread across workers
    instead, and a tracker that fails now names itself in the report rather than
    stopping the loop before the trackers after it are ever asked.
    """
    assert real_readers()[tracker], f"nothing reads {tracker}, so its rounds widen"
    chosen = real_round(tracker)
    assert not chosen.widened, (tracker, chosen.reason)
    assert "autoloop/tests/test_docs_merge.py" in chosen.selected, tracker


def test_a_change_to_the_audit_charters_toml_selects_exactly_what_it_did_before():
    """REQUIREMENT 2. `docs/audit_charters.toml` is a RUNTIME INPUT — the audit
    parses it (`[repo].audit_charters_file`) and `test_audit_charters.py`
    requires the shipped file to parse to exactly `DEFAULT_DOMAINS` — so a
    change there really does change behaviour.

    Asserted as an EQUALITY against the reference-token rule recomputed from the
    two functions select-02 did not touch, rather than as "it still selects
    something": a count would pass just as well if the set had been quietly
    replaced. The round holds no Python, so the whole selection is attribution
    and there is nothing else the equality could be measuring.
    """
    path = "docs/audit_charters.toml"
    assert not _is_prose_document(path)

    chosen = real_round(path)

    assert not chosen.widened, chosen.reason
    assert chosen.resolved == ()
    assert set(chosen.selected) == token_rule_tests(path)
    assert "autoloop/tests/test_audit_charters.py" in chosen.selected


def test_no_file_that_addresses_this_checkout_builds_a_document_name_dynamically():
    """The one gap the derivation cannot see, asserted ABSENT rather than
    argued away.

    A file that builds a document's name instead of spelling it —
    `f"{stem}.md"` — names it in no constant, so it is attributed to no
    document. That leaves the attributed set INCOMPLETE rather than empty, so
    the empty-attribution backstop does not fire and nothing says so: the
    silent failure this rule could have. It cannot be closed statically; what
    can be done is to fail the round that introduces one, here, with the file
    and line named.

    Scoped to files that can reach this checkout at all, because a dynamic name
    in a module that is always handed a root belongs to that root's tree, not
    to this one — `audit/executor.py`'s `f"docs/AUDIT_{date}.md"` writes into
    the repository under audit.
    """
    offenders = []
    for rel in sorted(real_graph().files):
        try:
            tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        if not _addresses_own_checkout(tree):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr) or not node.values:
                continue
            last = node.values[-1]
            if (
                isinstance(last, ast.Constant)
                and isinstance(last.value, str)
                and last.value.endswith(_PROSE_DOC_SUFFIX)
            ):
                offenders.append(f"{rel}:{node.lineno}")

    assert offenders == [], (
        "these build a prose document's name instead of naming it, so a change "
        "to that document would not select them: " + ", ".join(offenders)
    )


#: The measured rounds, by the names the task's own table gives them. The first
#: is one `autoloop/` module alone; the second is the SAME round with the change
#: note `CLAUDE.md` requires on top, which is what every real round looks like.
ROW_ONE = ("autoloop/validation.py",)
ROW_TWO = ("autoloop/validation.py", "docs/SUMMARY.md", "docs/TESTS.md")


def before_selection(paths) -> frozenset[str]:
    """`selected` for `paths` under select-01's rule, for EVERY path kind.

    Written out here rather than kept behind a flag in the selector: the
    comparison this file has to make is against a rule that no longer exists,
    and a switch that could restore it in production is a bigger surface than
    the measurement is worth.
    """
    graph = real_graph()
    resolved = [rel for rel in paths if rel in graph.files]
    unresolved = [rel for rel in paths if rel not in graph.files]
    tests: set[str] = set()
    if resolved:
        tests |= {rel for rel in graph.reachable_from(resolved) if _is_test_file(rel)}
    for rel in unresolved:
        tests |= token_rule_tests(rel)
    return frozenset(tests)


def test_the_change_note_round_no_longer_selects_the_whole_suite():
    """DONE-WHEN, measured on the two rounds the task's table names.

    Measured 2026-08-27 in this extracted checkout (the brief's numbers were
    taken in the repository this was split out of, which also had a root
    `tests/` tree and a `docs/AUTOLOOP.md`; neither exists here, so its second
    column is not reproducible and is not invented):

      changed paths                          before      after
      autoloop/validation.py alone           80/93       80/93
      + docs/TESTS.md, docs/SUMMARY.md       93/93       80/93
      val-04-shaped (4 modules + both)       93/93       80/93
      docs-only (all six trackers)           93/93       72/93
      docs/audit_charters.toml               93/93       93/93  (unchanged)

    The change note now adds NOTHING to the round it rides on: every test that
    reads a tracker is already reachable from `autoloop/validation.py`, so the
    second row is the first row exactly. The docs-only row is 72 rather than a
    handful because `autoloop/dashboard.py` sweeps directories with the bare
    code literal `"*"` (audit-runs directories, outside the graph) and that
    pattern matches any basename, so the glob arm seeds it and its importers
    come along. That is the conservative direction on purpose.

    The val-04-shaped row is a STAND-IN: this round has no git access, so
    val-04's actual changed-path set could not be read, and the four modules
    named are a round of that shape rather than that round.

    The assertions are relational rather than absolute so this states a
    property instead of a snapshot: adding the change note may only add the
    tests that READ a tracker, the union must stay a strict subset of the
    suite, and the second round must select strictly fewer than select-01's
    rule did — which selected every test file.
    """
    graph = real_graph()
    total = len(graph.test_files)
    row_one = real_round(*ROW_ONE)
    row_two = real_round(*ROW_TWO)

    assert not row_one.widened, row_one.reason
    assert not row_two.widened, row_two.reason
    assert set(row_one.selected) <= set(row_two.selected), "a note cannot DESELECT"
    assert len(row_two.selected) < total, "the round this whole task exists for"
    before_two = before_selection(ROW_TWO)
    assert len(before_two) >= total - 3, (
        "the defect being fixed: under the token rule the change note selected "
        "essentially the whole suite. If that is no longer true of this "
        "checkout, the comparison below is measuring something else and the "
        "numbers in this docstring are stale"
    )
    assert len(row_two.selected) < len(before_two)

    readers = _files_reading_documents(
        REPO_ROOT, sorted(graph.files), ["docs/SUMMARY.md", "docs/TESTS.md"]
    )
    from_the_note = set(row_two.selected) - set(row_one.selected)
    reachable_from_readers = {
        rel
        for seeds in readers.values()
        for rel in graph.reachable_from(seeds)
        if _is_test_file(rel)
    }
    assert from_the_note <= reachable_from_readers, sorted(
        from_the_note - reachable_from_readers
    )


# ---------------------------------------------------------------------------
# A glob that discriminates nothing is not a document name.
# ---------------------------------------------------------------------------


def test_a_glob_of_only_wildcards_names_no_document():
    """`"*"` matches every path, so it is evidence about no document at all.

    The carve-out above matches a glob rather than comparing it, because a
    directory sweep reads files it never spells out. That branch has to know
    the difference between a sweep that CAN reach documents and one that merely
    happens to contain a wildcard.
    """
    for pattern in ("*", "**", "*/*", "**/*"):
        assert not _glob_constrains(pattern), pattern
        assert not _names_document({pattern}, "docs/SUMMARY.md", "SUMMARY.md")

    # ...and every sweep this branch exists for still names what it reaches.
    for pattern in ("*.md", "docs/*.md", "docs/AUDIT_*.md"):
        assert _glob_constrains(pattern), pattern
    assert _names_document({"*.md"}, "docs/SUMMARY.md", "SUMMARY.md")
    assert _names_document({"docs/AUDIT_*.md"}, "docs/AUDIT_2026.md", "AUDIT_2026.md")
    assert not _names_document({"docs/AUDIT_*.md"}, "docs/SUMMARY.md", "SUMMARY.md")


def test_dashboard_is_not_a_reader_of_the_trackers_it_never_opens():
    """The measured case, pinned against the module that produced it.

    `dashboard.py` calls `audit_dir.glob("*")` to list AUDIT RUN directories.
    The only documentary path it evaluates is `docs/AUDIT_*.md`, which is the
    audit-report glob and matches no tracker. Before `_glob_constrains`, that
    bare `"*"` matched every tracker and made this module a declared reader of
    all six; because it is imported across the suite, the closure over it then
    selected 72 of 93 test files on a change to prose no test can observe.

    Asserted on the SHIPPED module rather than a fixture: the defect was a
    property of this checkout's real source, and a fixture would have passed
    throughout.
    """
    dashboard = REPO_ROOT / "autoloop" / "dashboard.py"
    strings = _code_strings(ast.parse(dashboard.read_text(encoding="utf-8")))
    assert "*" in strings, (
        "dashboard.py no longer evaluates a bare '*'; this test still passes "
        "but has stopped pinning the case it was written for"
    )
    for rel in TRACKER_PATHS:
        assert not _names_document(strings, rel, Path(rel).name), rel

    for rel, found in real_readers().items():
        assert "autoloop/dashboard.py" not in found, (rel, sorted(found))
