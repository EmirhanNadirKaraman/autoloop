---
id: ctx-incident-rate-limit-as-browser-fault
kind: incident
status: resolved
summary: An account-level throttle was reported as a lost browser session and burned a task's attempt budget overnight.
source_paths: autoloop/errors.py autoloop/orchestrator.py autoloop/policy.py autoloop/state.py
test_paths: autoloop/tests/test_rounds_and_restart.py
task_ids: pkt-03
last_verified_commit: UNSTAMPED
superseded_by:
---

## Symptom

Overnight on 2026-08-14/15, from 07:56 onward, every round failed with the same
message: `browser session lost: Locator.click: Timeout 30000ms exceeded.
waiting for locator("#prompt-textarea")`. The loop restarted Chrome, retried,
and reported the retry's failure the same way. `pkt-03` burned through its
five-attempt ceiling without ever reaching an approved review.

## Reproduction

The provider throttles the ACCOUNT and covers the page with its "Too many
requests" overlay. The loop had no selector for that overlay, so it saw only a
composer that would not take a click. The condition is server-side and answers
to a timer, not to anything the client does — and every recovery step the loop
took (restart, re-attach, retry) is itself another request, so the recovery
deepened the fault it was recovering from.

The distinction the whole escalation turns on is pinned in the test file's
`_ThrottleAwareClient`: dismissing the modal always succeeds and proves nothing.

## Root cause

One fault class was being read as another. Nothing about the browser was
broken, but the failure arrived through the browser and was classified by where
it arrived rather than by what it was — so it inherited the browser recovery
(drop the client, restart Chrome, retry) and the browser failure budget, both of
which are wrong for an account-level limit.

## Resolution

`errors.RateLimitedError` is deliberately NOT a `BrowserError`, so it never
reaches `_handle_browser_failure` at all. It routes to
`orchestrator._handle_rate_limited`, whose remedy is to WAIT, with its own
bounded budget `policy.max_rate_limit_backoffs`. `state.py` carries the matching
restart-cooldown exemption, bounded by the same number, on the principle that a
failure nobody could have recovered from must not be charged to the budget that
decides recovery is hopeless.

`errors.QuotaExhaustedError` stays separate again: a spent plan allowance is an
account condition whose remedy is to park or hand over to the fallback provider,
not to wait.

## Regression test

`autoloop/tests/test_rounds_and_restart.py`, the section headed "account rate
limit vs the browser recovery". It drives `_handle_rate_limited` directly, and
its fake keeps the limit in force after the overlay is dismissed — a fake that
lifted the limit on dismissal would let a reset-on-dismissal implementation pass
while, in production, the delay never doubled and the loop never parked.

## Affected commits

None quoted. This record was seeded by an agent with no shell and no git access,
so no sha here would be a measurement; the incident predates this repository's
extraction from its parent, and the fix is identified by the source paths above
and by task `pkt-03`. A later round with a checkout may add the commits.

The browser transport itself has since been retired (`autoloop/browser/` is
gone), which changes how the fault would arrive but not the classification rule
this incident established.
