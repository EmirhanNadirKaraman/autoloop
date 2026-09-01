# Feature records

One record per feature that has a boundary worth stating. A feature record
carries intent and boundaries, entry points, invariants, data flow, the tests
and decisions that bind it, and its known failure modes — each as a POINTER at
the code or the tracker that holds the detail, never as a copy of it.

Those six headings are required: `context_records.REQUIRED_SECTIONS` is the
list, and a record missing one is refused with the heading named. A feature
record must also name at least one test path — a feature nothing pins is a claim,
not a feature.

Add the record to `../index.md` in the same commit, or the loader refuses it.
