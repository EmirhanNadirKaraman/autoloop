# COMMON_ERRORS.md

Symptom-first log of errors actually hit here. Grep it by the error text in
front of you before debugging from scratch. Add an entry whenever something
new breaks, in the same change that fixes it.

Several entries are traps where the obvious fix is wrong, so it is worth a look
*before* "fixing" a lint or test failure, not only after being stuck.

### `pytest` reorders tests and unrelated cases start failing

`pytest-randomly` is installed in this environment. `pytest.ini` disables it
with `-p no:randomly`. Running pytest in a way that bypasses that config
reintroduces the reordering. This is not flakiness to chase; restore the flag.

### A test passes alone and fails in the full suite

It may be one of the `@pytest.mark.isolated` cases, which `pytest.ini`
deselects by default. Run it in a dedicated process rather than removing the
marker.

## Change notes — append ONE new line at the END of this file

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-27 | split | extracted from language-app with git filter-repo, 414 commits of history preserved; this tracker seeded here. |
