# Lesson records

A lesson exists ONLY when one of three things is true:

* the same mistake has happened MORE THAN ONCE;
* a reviewer named it as a reusable failure pattern; or
* it exposed a non-obvious project invariant.

A one-off mistake must never become permanent project context. Every round makes
mistakes, and a directory that collects them is noise that later rounds have to
read.

A lesson record carries `## Evidence` — the occurrences, each pointing at where
it is recorded — and `## Prevention rule`, ONE concrete rule someone can apply
to the next change. Both headings are required, as are the task ids the evidence
comes from.

Add the record to `../index.md` in the same commit, or the loader refuses it.
