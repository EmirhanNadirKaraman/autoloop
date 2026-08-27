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
