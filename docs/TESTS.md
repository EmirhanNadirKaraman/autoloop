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
