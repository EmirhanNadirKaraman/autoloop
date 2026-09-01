# Decision records

One record per decision that constrains later work — what was decided, what was
rejected and why, and what it costs. The consequences half is the part that
earns the record: a decision whose downsides are unstated reads as free.

No headings are required for this kind, deliberately. The shape of a decision
record is not specified anywhere in this repository yet, and the validator does
not invent one; a non-empty body and real source paths are what it checks.

`docs/SECURITY.md` is the model for the discipline — stable id, explicit status,
evidence, a check a reader re-runs — and a decision about a security control
belongs there, with a record here pointing at it if it also constrains ordinary
work.

Add the record to `../index.md` in the same commit, or the loader refuses it.
