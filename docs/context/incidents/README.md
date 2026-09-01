# Incident records

One record per incident that changed something. An incident record carries the
observable symptom, the conditions that reproduce it, the root cause, the
resolution, the regression test that would catch it again, and the affected
commits — all six headings are required.

An incident record names the task ids it is evidence from. It is not a round
summary and not a write-up of how the fix was implemented: what happened, why,
and what stops it recurring.

If no sha can be resolved for the affected commits, say so and say why. A sha
nobody read is not evidence.

Add the record to `../index.md` in the same commit, or the loader refuses it.
