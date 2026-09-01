---
id: ctx-project-autoloop
kind: project
status: active
summary: What this repository is, and which document answers which question about it.
source_paths: CLAUDE.md autoloop/orchestrator.py autoloop/tasks.py docs/SUMMARY.md docs/AUTOLOOP.md
test_paths: autoloop/tests/test_orchestrator.py autoloop/tests/test_tasks.py
task_ids: ctx-02
last_verified_commit: UNSTAMPED
superseded_by:
---

## What this is

`autoloop` is an autonomous task loop: it takes a queue of tasks, dispatches
each to an implementation agent in an isolated worker repository, has the result
reviewed, validates it, commits it and merges it into an integration branch. It
is the tool that maintains itself — most tasks in its queue modify its own
source, so nearly every claim in this tree is a claim about the code that made
it.

The package deliberately stayed a subdirectory when it was extracted from its
parent repository, so `from autoloop.x import y` and every test's path
arithmetic keep working. `CLAUDE.md` is the authority on that and on the
commands a change is validated with; this record does not repeat them.

## Where the detail lives

| Question | Document |
|---|---|
| Which file do I change for X? | `docs/SUMMARY.md` — a file-to-responsibility index. |
| How does mechanism X behave, and when did it change? | `docs/AUTOLOOP.md`, by section heading. |
| What is unsafe, and what was decided about it? | `docs/SECURITY.md` — ids, status, evidence, a check to re-run. |
| I am seeing symptom X. | `docs/COMMON_ERRORS.md`. |
| What is covered, and what is known to fail? | `docs/TESTS.md`. |
| What shape is a persisted record? | `docs/SCHEMA.md`. |

Those six are trackers. Four of them end in an append-only change-note section
and are extended one line at a time; `ctx-feature-append-only-change-notes`
records why, and `CLAUDE.md` states the rule a task has to follow.

## Boundaries

This record is a map, not a summary. It carries no behaviour anyone could act
on that is not in the documents above, and it must not grow into a second copy
of them: the failure this whole tree exists to avoid is prose nobody can check
drifting away from the code while still reading as true.

What is NOT here, deliberately: anything about a specific round, a conversation,
or one task's implementation. A round is history, and history is in the commits
and in the trackers' change notes.
