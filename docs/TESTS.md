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
