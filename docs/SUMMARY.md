# SUMMARY.md

File-level index of this repository. "Where do I look to change X?" is answered
here. Update it whenever a file is added, removed, or changes responsibility.

| Path | Purpose |
|---|---|
| `autoloop/orchestrator.py` | The loop's spine: phases, dispatch, review, merge, self-upgrade. |
| `autoloop/cli.py` | Every subcommand, and the operator surface. |
| `autoloop/tasks.py` | Task registry, dependency graph, approved paths. |
| `autoloop/validation.py` | Validation commands and per-commit test selection. |
| `autoloop/packet.py` | Review-packet construction. |
| `autoloop/git_gateway.py` | Every git call, behind a policy. |
| `autoloop/note_merge.py` | Append-only change-note conflict resolution. |
| `autoloop/codex/` | The Codex transports (subprocess and app-server). |
| `autoloop/browser/` | Retired browser transport; kept only until brw-19 removes it. |
| `autoloop/audit/` | Read-only audit agents and task generation. |

## Change notes — append ONE new line at the END of this file

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-27 | split | extracted from language-app with git filter-repo, 414 commits of history preserved; this tracker seeded here. |
| 2026-08-27 | select-02 | `autoloop/validation.py`: a changed `.md` is now attributed by `_files_reading_documents` — the files that READ it (exact name in evaluated code, plus `__file__`) — instead of `_reference_tokens`, whose bare `docs` and `.md` tokens made the change note every task must write select the whole suite. `.toml` and every other unresolvable path keep the token rule; the closure, the union and every widening rule are unchanged, and a document nothing reads still widens. |
| 2026-08-27 | select-03 | `autoloop/validation.py`: `_names_document`'s glob branch now requires the pattern to keep a literal character. A glob of only wildcards matches every path, so it names no document. `dashboard.py:4026`'s `audit_dir.glob("*")` — a listing of audit RUN directories — matched all six trackers, made that module a declared reader of each, and the closure over it then selected 72 of 93 test files on any prose change. Measured: docs-only 72 -> 20, `validation.py` plus both trackers 80 unchanged, `docs/audit_charters.toml` 93/93 unchanged. `*.md` and `docs/AUDIT_*.md` are unaffected. |
| 2026-08-28 | perf-01 | `autoloop/validation.py`: `_file_is_opaque` and `_imported_modules` each walked every parsed file separately, and `ast.walk` was 63% of a `select_validation_commands` call. They are now one `_scan_module` returning `(opaque, imported)` from a single walk: 0.98s -> 0.63s over this checkout's 159 files. WHICH nodes count did not change, and the two functions' agreement with the merged one was checked file-by-file across the whole checkout before the split was removed. Prose naming either function was updated to name `_scan_module`. |
| 2026-08-28 | perf-01 | `autoloop/validation.py`: two more tree walks out of the selection path. `_code_strings` collected docstring ids and string constants in separate `ast.walk` passes and now does one, applying the prose filter after it. `_addresses_own_checkout` is not called at all when the source lacks the literal token `__file__`: an identifier cannot be spelled any other way, so absence of the text is a necessary condition and the skipped walk could not have found a `Name` — 27 of 159 files still need it. Both checked against the previous implementations over every file in the checkout. Six real-repository selections: 19.69s -> 15.09s. |
