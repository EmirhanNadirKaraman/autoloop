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
| 2026-08-30 | upgrade-01 | Symptom: the loop is gone and the last transcript line is `self_upgrade_boundary`, with nothing after it. Not a crash and not the lock: a non-`--continuous` launch (`run`, `--retry`, `--answer`, `resume`) reached the boundary in `cli._run_locked`, which returned 0 mid-session, while `_cmd_run`'s `finally` published the heartbeat `stopped` — outside `ATTENTION_STATUSES`, so nothing alarmed either. Every outcome now logs `self_upgrade_<outcome>`; a boundary with no entry after it means the running build predates 2026-08-30. The record stays pending, so `python -m autoloop start` still performs the upgrade. |
| 2026-08-31 | upgrade-01 | Symptom: a loop-code merge never runs and `pending_upgrade.json` says `preflight_failed`, `unapplicable` or `exec_failed`. A build older than 2026-08-31 SETTLED that record and no boundary offers it — only `pending` is offered. Clear the file, or merge the fix (which rewrites the record with a new `base_sha`), to re-arm it. Those three outcomes now leave the record `pending` and the next process retries them; `execed` in that file is different and correct — a successor is running, or `_confirm_self_upgrade` has not retired it yet. |
| 2026-08-31 | upgrade-01 | Symptom: the loop dies with `TypeError: unhashable type` or `'dict' object is not subscriptable` immediately after `self_upgrade_boundary`. Cause: `pending_upgrade.json` was hand-edited or half-written and its `base_sha` is not a string — `UpgradeStore.load` coerces nothing, so the value reached `set.add`, a decline's `in` test, or the `[:12]` slice on the exec path. Every reader of that field now asks `auto_merge.upgrade_bound_sha`; a record it rejects is refused at the boundary (`self_upgrade_none`, naming the type it found) and left untouched on disk for you to fix. |
| 2026-08-31 | upgrade-01 | Symptom: in continuous mode `self_upgrade_boundary` and a refusal entry repeat every round for the same `base_sha`, and no task progresses. Cause: `pending_upgrade.json` changed between the loop's read on the way into the boundary and the decision's own read of it, so the process declined the record it had READ and not the one it refused — and only a declined sha stops `_self_upgrade_due` offering again. The decision now returns which record it acted on (`cli.UpgradeOutcome`) and both shas are declined per process. A repeat after that names a different `base_sha`, which is a genuinely new merge. |
| 2026-08-31 | val-08 | `--lf`, `--ff` and `--sw` are served by pytest's cacheprovider plugin, so any of them beside `-p no:cacheprovider` is `exit 4` on an unrecognised argument BEFORE a test runs — which reads as a broken suite rather than as a bad flag. When a cache is wanted, MOVE it with `-o cache_dir=<absolute path>` instead of disabling the plugin: measured 2026-08-28 on this checkout, that leaves `git status` byte-identical and writes the cache to the given path. `autoloop/validation.py` makes the bad pair unreachable by injecting the rerun flag only inside the branch that enabled the cache. |
| 2026-08-31 | val-08 | A `VERIFIED:` claim in a docstring can be true of the PRE-SPLIT repository and false here. `implement_executor.py` said `.gitignore` lists neither `.ruff_cache` nor `.pytest_cache` (verified 2026-08-23); this checkout's lists BOTH, at lines 51 and 207, because the 2026-08-27 extraction brought a standard Python `.gitignore` with it. Nothing re-checked the sentence and it was being read as evidence about validation residue. Corrected in place rather than deleted, since briefs quote it. Check a dated claim against THIS tree before building on it. |
| 2026-08-31 | val-08 | `Path.is_relative_to` is string arithmetic about the path you HAND it, so a containment check has two ways to be quietly wrong. This one compared the temp cache against the directory validation RUNS in — a declared `validation_cwd` puts that BELOW the worker repo, so a sibling of it is outside the check and inside the tree the gate reads. It also followed no symlinks: a `TMPDIR` linked into the checkout is lexically outside and physically inside. Compare against the ROOT, resolved, both directions. |
| 2026-08-31 | val-08 | A rewrite that DEFERS to an explicit operator flag can be a fail-open. `-o cache_dir=` looked like an operator decision to respect; it is a relative path resolved against pytest's ROOTDIR (into the tree being graded) or a shared absolute one two tasks would both write, and deferring also dropped the `-p no:cacheprovider` that had been neutralising it. Rule: when a guarantee is about WHERE bytes land, override the setting or refuse the run — and decide the refusal by SUBSTRING, since a structural parser answers "nothing there" for the spellings it cannot read (`-qocache_dir=.x` is `-o cache_dir=.x`). |
| 2026-08-31 | val-08 | A file's EXISTENCE is not provenance. `lastfailed` proves some earlier run recorded failures, never that the run just finished did — and validation stops at the first failing command, so a round whose `ruff` broke left pytest's list from two edits back to narrow the NEXT run. Clear the record before each run that could write one and require it absent at launch. Where clearing is impossible (a `--lf` run reads the file it would clear) fail closed rather than infer: pytest rewrites `lastfailed` only when the set CHANGED, so "unchanged" cannot be told from "pytest never ran". |
| 2026-08-31 | merge-08b | Symptom: a task asks the implementing agent to run `merge-backlog` and report the backlog clear. It cannot, and not merely for want of a shell. `cli._cmd_merge_backlog` takes the single-instance loop lock and MOVES and pushes the base head, so it is unrunnable from inside a round a live loop is driving; and what it reads — the registry and execution records under `[paths].state_dir` — sits outside the worker repo, which config REQUIRES to live elsewhere. Reconciling the backlog is an operator step. A worker can verify a vendored artifact and nothing further. |
| 2026-08-31 | brw-19b | Deleting the last emitter of a blocker `code=` also strands a key `cli._RESOLUTION_PRECONDITIONS` still holds: `test_m1_hardening.py::test_every_precondition_key_matches_a_real_emitted_code` AST-walks `orchestrator.py` for every `code=` a blocker emitter is given, so the round goes red in a file it never touched. Check that walk before removing a park; remove the park and its precondition together, or neither. brw-19b left `_recover_unattachable_browser` dormant rather than deleted for exactly this — both halves of the pair were outside its scope. |
