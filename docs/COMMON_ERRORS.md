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
| 2026-08-31 | val-08 | `--lf`, `--ff` and `--sw` are served by pytest's cacheprovider plugin, so any of them beside `-p no:cacheprovider` is `exit 4` on an unrecognised argument BEFORE a test runs — which reads as a broken suite rather than as a bad flag. When a cache is wanted, MOVE it with `-o cache_dir=<absolute path>` instead of disabling the plugin: measured 2026-08-28 on this checkout, that leaves `git status` byte-identical and writes the cache to the given path. `autoloop/validation.py` makes the bad pair unreachable by injecting the rerun flag only inside the branch that enabled the cache. |
| 2026-08-31 | val-08 | A `VERIFIED:` claim in a docstring can be true of the PRE-SPLIT repository and false here. `implement_executor.py` said `.gitignore` lists neither `.ruff_cache` nor `.pytest_cache` (verified 2026-08-23); this checkout's lists BOTH, at lines 51 and 207, because the 2026-08-27 extraction brought a standard Python `.gitignore` with it. Nothing re-checked the sentence and it was being read as evidence about validation residue. Corrected in place rather than deleted, since briefs quote it. Check a dated claim against THIS tree before building on it. |
| 2026-08-31 | val-08 | `Path.is_relative_to` is string arithmetic about the path you HAND it, so a containment check has two ways to be quietly wrong. This one compared the temp cache against the directory validation RUNS in — a declared `validation_cwd` puts that BELOW the worker repo, so a sibling of it is outside the check and inside the tree the gate reads. It also followed no symlinks: a `TMPDIR` linked into the checkout is lexically outside and physically inside. Compare against the ROOT, resolved, both directions. |
