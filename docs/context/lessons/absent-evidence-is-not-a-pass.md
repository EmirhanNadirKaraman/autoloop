---
id: ctx-lesson-absent-evidence-is-not-a-pass
kind: lesson
status: active
summary: A recheck that filters evidence by name must require the evidence to be present; absence has twice read as health.
source_paths: autoloop/cli.py autoloop/doctor.py docs/SECURITY.md
test_paths: autoloop/tests/test_doctor.py autoloop/tests/test_m1_hardening.py autoloop/tests/test_blockers.py
task_ids: brw-19c brw-19d
last_verified_commit: UNSTAMPED
superseded_by:
---

## Evidence

The same defect, twice in one day, in adjacent functions — and named a third
time by a reviewer as a reusable pattern, which is why it is recorded here
instead of as one more incident.

1. **brw-19c** (`docs/SECURITY.md`, 2026-08-31). The blocker recheck behind
   `login_expired` filtered doctor's rows by a tuple of names and treated the
   result as the environment's verdict. The same round deleted the checks that
   tuple named. A tuple naming rows nobody emits selects nothing, an empty
   selection contains no failures, and no failures read as health — so a blocker
   recording an unresolved ENVIRONMENT fault became resolvable by answer text
   alone. Nothing failed while it happened.
2. **brw-19d** (`docs/SECURITY.md`, same day). The successor function had the
   same shape: it filtered by name over whatever the sweep produced, so once
   doctor stopped emitting the filtered rows, `login_expired` and
   `submission_ambiguous` were clearable with nothing verified.
3. **Autoloop M1 finding #7**, cited by `cli.py` at both sites: a recheck wired
   to evidence that cannot answer the question it guards. `_precondition_git_
   health` exists because the git-failure blocker was answered by the transport
   sweep — a logged-out seat is not a reason to hold a git question open, and a
   logged-in one is not a reason to close it.

The invariant this exposes is not obvious from either function: in this codebase
the ROWS a sweep emits are not a stable interface, so any check keyed on row
names is one unrelated deletion away from selecting nothing.

## Prevention rule

**A guard must require its evidence to be PRESENT and affirmative, and an empty
match must refuse.** Concretely, in the shape this repository now uses
(`cli._precondition_transport_live`):

* name the rows that MUST appear (`_TRANSPORT_PRECONDITION_CHECKS`); a missing
  one refuses and says which was absent;
* a present row that is not `ok` refuses, naming it and its detail;
* a sweep that RAISES refuses — nothing was verified, so nothing is cleared;
* and the "these rows really are emitted" half is derived from a REAL sweep in a
  test (`test_doctor.py::test_doctor_emits_every_required_transport_precondition_
  row`), not asserted in a comment, so deleting a row fails a test instead of
  quietly disarming the guard.

Applying it to a new check is one question: *if the thing I am reading were
absent or unreadable, would this return "fine"?* If yes, the check is not a
check yet.
