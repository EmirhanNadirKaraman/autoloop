# SECURITY.md

Living security tracker: open findings, resolved findings, and the controls
relied on. Read it before changing anything that touches git operations,
subprocess invocation, the publisher, or an agent's write scope.

Every finding carries four things, or it rots into an untested claim:
`file:line`, a severity, a one-line verification check, and a suggested fix.

## Verified strengths

- Git operations run through `GitGateway` behind a policy; force pushes and
  destructive commands are denied rather than merely discouraged.
- `protected_branches` refuses direct pushes to the base branch.
- The reviewer gets no repository access: its working directory is outside the
  checkout and its prompt is self-contained.
- The escape detector brackets every write-capable agent call.

## Open findings

_None recorded yet in this repository._

## Change notes — append ONE new line at the END of this file

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-27 | split | extracted from language-app with git filter-repo, 414 commits of history preserved; this tracker seeded here. |
| 2026-08-31 | brw-19c | `autoloop/cli.py:_precondition_browser` — a name-whitelist recheck is disarmed by deleting the checks it names, and nothing fails while it happens. Severity: medium; a blocker recording an unresolved ENVIRONMENT fault becomes resolvable by answer text alone, which is Autoloop M1 finding #7's exact shape. Verify: answering a `login_expired` blocker must be REFUSED while the configured provider cannot be opened. Fixed here — the whitelist is `_BROWSER_PRECONDITION_CHECKS`, led by `primary_live`, which `run_doctor` still emits; `cdp`/`playwright` are retained but dead. |
