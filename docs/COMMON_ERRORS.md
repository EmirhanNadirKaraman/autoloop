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
