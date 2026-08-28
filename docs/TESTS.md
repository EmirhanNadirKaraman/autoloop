# TESTS.md

What is and is not covered, plus any known pre-existing failures.

Run the suite with `python3 -m pytest autoloop/tests`. `pytest.ini` supplies
`-p no:randomly` and `-m "not isolated"`; both are load-bearing and explained
there.

| Area | Where |
|---|---|
| Loop phases, dispatch, review | `autoloop/tests/test_orchestrator.py` |
| Task registry and graph | `autoloop/tests/test_tasks.py` |
| Test selection | `autoloop/tests/test_test_selection.py` |
| Change-note merging | `autoloop/tests/test_docs_merge.py` |
| Codex transports | `autoloop/tests/test_codex_provider.py`, `test_codex_app_server.py` |

**Known failing on a fresh split (2026-08-27):** roughly 19 test files resolve
`Path(__file__).resolve().parents[2]` and assert about the repository that
contains them. They were written against the parent repository and need
fixture repositories instead; `test_audit_charters.py` already shows the shape.

## Change notes — append ONE new line at the END of this file

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-27 | split | extracted from language-app with git filter-repo, 414 commits of history preserved; this tracker seeded here. |
| 2026-08-27 | select-02 | new `autoloop/tests/test_prose_doc_selection.py` pins the prose-document carve-out in test selection: `test_docs_merge.py` still runs on a docs-only round, `docs/audit_charters.toml` selects exactly what the untouched reference-token rule selects, a newly added doc-reading test is picked up with no list edited, and a document nothing reads still widens. `test_test_selection.py`'s "every test naming a tracker runs on a tracker change" claim is reversed there and corrected in place. |
| 2026-08-27 | select-03 | `test_prose_doc_selection.py` gains two cases: a glob of only wildcards names no document while `*.md` and `docs/AUDIT_*.md` still do; and `autoloop/dashboard.py` is not a reader of the trackers it never opens — asserted against the SHIPPED module rather than a fixture, because the defect was a property of the real source and a fixture would have passed throughout. Both fail with the fix reverted. |
| 2026-08-28 | perf-01 | `test_prose_doc_selection.py`'s `test_every_tracker_has_a_reader_so_a_change_note_round_never_widens` is parametrised over `TRACKER_PATHS` rather than looping inside one test: each tracker costs a full real-repository selection, and six in series made it the slowest test in the suite at 104s under `-n auto`, all on one worker while the rest idled. A cached `real_readers()` answers the reader question once for the file, which also drops a second `build_import_graph` from `test_dashboard_is_not_a_reader_of_the_trackers_it_never_opens`. Whole suite on 8 cores, all of perf-01 measured back to back against an otherwise idle machine: 446.37s -> 418.18s. |
| 2026-08-28 | perf-01 | New `autoloop/tests/gitrepo.py` builds a test repository by copying a per-process template instead of running `init`, three `config`, `add` and `commit` each time. Stdlib only, so it adds no edge to the graph the selector reads, and the cache lives under a directory the PROCESS made, so xdist workers neither share nor race on it. 30 files whose builder matched that shape byte-for-byte were converted mechanically, carrying through each one's branch, file, content, identity and message — author and message are part of the commit object. |
| 2026-08-28 | perf-01 | Which builders to convert was decided by measurement: `PYTEST_CURRENT_TEST` attributed all 2,206 `git init` calls to their tests, and those 30 files cover 1,461. Per run 2,206 -> 1,218. A shared initial sha is not a new hazard — one-second commit granularity already made independent builds byte-identical, and a branch is a ref that never enters the commit. On 8 cores this is lost in noise (the suite sits at 606% of 800%, so it is not CPU-bound); at `-n 4`, the CI shape and 88% utilisation, 545.73s -> 509.83s. |
| 2026-08-28 | perf-01 | `scripts/check_selection_soundness.py` runs the suite under per-test coverage contexts and asserts each test's statically derived module set, closed over the import graph, COVERS what it actually executed. Coverage is the check, never the mechanism: a recorded map is stale the moment code moves and its failure mode is silent under-selection. Its own CI job (`selection-soundness`), parallel to the test job so it adds no wall time. Contexts survive `-n auto`; serially it takes over an hour. |
| 2026-08-28 | perf-01 | That gate found four defects the static analysis reported as fine, each a silent under-selection: sibling test-module imports (`autoloop/tests` is on sys.path, no `__init__.py`), classes that build wiring in `__init__`, autouse fixtures no test's text points at, and the check omitting the forward import closure. 105 violations -> 0 over 3,865 tests. `test_per_test_deps.py` pins each as a fixture-only case. An earlier 276-test sample scored 0 and proved nothing — it contained none of those shapes, so the gate must run over the WHOLE suite. |
