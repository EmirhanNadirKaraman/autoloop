# CLAUDE.md

Guide for Claude (and humans) working in this repository.

## What this is

`autoloop` — an autonomous task loop. It takes a queue of tasks, dispatches each
to an implementation agent in an isolated worker repository, has the result
reviewed, validates it, commits it, and merges it into an integration branch.
It is the tool that maintains itself; most tasks in its queue modify its own
source.

Extracted from the `language-app` repository on 2026-08-27 with
`git filter-repo --path autoloop/`, preserving 414 commits of history. The
package deliberately stayed a subdirectory so `from autoloop.x import y` and
every test's path arithmetic keep working.

## Layout

- `autoloop/` — the package. `orchestrator.py` is the spine; start there.
- `autoloop/tests/` — the suite (~4,200 tests).
- `docs/` — the trackers. Four of them are append-only; see §Change notes.

## Validating a change

Three commands, and all three must pass:

```bash
ruff check .
python3 -m pytest autoloop/tests -q -n auto -p no:cacheprovider
```

`pytest.ini` supplies `-p no:randomly` and `-m "not isolated"`. Both are
load-bearing and explained in that file. `-p no:cacheprovider` matters because
pytest writes `.pytest_cache` the moment a test fails, which the loop's own
post-commit gate reports as validation having mutated the worker tree.

## Change notes

`docs/SUMMARY.md`, `docs/TESTS.md`, `docs/SECURITY.md` and
`docs/COMMON_ERRORS.md` each end with an append-only section opened by a
`CHANGE-NOTES` marker comment.

**Record a change note as ONE NEW LINE, appended at the END of the tracker.
Never grow, edit or reorder an existing line.** This is a merge requirement,
not a style preference: `autoloop/note_merge.py` combines two branches'
appended lines automatically, and that resolver only fires when each side left
every pre-existing line byte-identical and added nothing outside the section.
Breaking either rule stops a parallel merge exactly like a source conflict.

- Adding a new table row for a file or test you touched is a new line, and fine.
- Anything else goes below the marker: one line, `| date | task-id | note |`,
  appended after the last one. Nothing may follow it.
- Each line has a hard length limit enforced by
  `autoloop/tests/test_docs_merge.py`. The number lives in exactly one place,
  `autoloop.note_merge.MAX_NOTE_LINE_CHARS` — read it there rather than copying
  it, because a second copy agrees today and silently disagrees the first time
  it moves. If a note does not fit, append a second line.
- Do not append a clause to an existing row to say what your task did. Split it
  into a second line instead.
- Refer to the marker as `CHANGE-NOTES`; never write the comment out in full a
  second time inside one of those four files. The resolver requires it exactly
  once per tracker, and a second copy switches auto-resolution off silently.
- `merge=union` was tried for this and removed the same day; see
  `.gitattributes` for the two measured reasons. Do not reintroduce it.

`CLAUDE.md` and `docs/SCHEMA.md` are also trackers, but have no append-only
section and nothing auto-resolves them — a conflict there stops the merge
deliberately, because they carry claims that need a human.

## Conventions

- Commit messages never mention AI tooling.
- Migrations of state files are append-only; never rewrite a shipped record.
- Every finding in `docs/SECURITY.md` carries `file:line`, a severity, a
  verification check, and a suggested fix.
