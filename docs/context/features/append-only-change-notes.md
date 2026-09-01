---
id: ctx-feature-append-only-change-notes
kind: feature
status: active
summary: Why every tracker ends in an append-only ledger, and what makes two branches' notes merge without a human.
source_paths: autoloop/note_merge.py autoloop/auto_merge.py autoloop/orchestrator.py autoloop/implement_executor.py CLAUDE.md .gitattributes
test_paths: autoloop/tests/test_docs_merge.py autoloop/tests/test_base_refresh_notes.py
task_ids: ctx-02
last_verified_commit: UNSTAMPED
superseded_by:
---

## Intent and boundaries

Every task in this repository writes a change note to the documentation
trackers, and tasks run in parallel. Two branches appending to the same file is
a git conflict on every merge — so the trackers are shaped as an APPEND-ONLY
ledger and a resolver combines two sides' additions automatically.

The boundary is narrow on purpose: the resolver reconciles ONLY the region after
the `CHANGE-NOTES` marker, only when both sides merely appended, and only for
the four files named in `note_merge.NOTE_TRACKERS`. Everything before the marker
is ordinary prose that git merges or conflicts as usual. `CLAUDE.md` and
`docs/SCHEMA.md` are trackers a task may write and are deliberately NOT in that
list: they have no append-only section, so a collision in either still stops the
sweep and reaches a human.

## Entry points

* `note_merge.resolve_note_append(base, ours, theirs, merged, lead=...)` — the
  decision. Returns the combined text, or `None` to leave the conflict standing.
* `note_merge.combine_conflicted_notes(git, conflicts, message)` — the in-merge
  driver, called from `auto_merge` (task into base) and from the base-refresh
  direction in `orchestrator.py`.
* `note_merge.MAX_NOTE_LINE_CHARS` — the one place the line-length limit lives.
  `implement_executor._authoring_rules` reads it at render time so the rule
  arrives in the agent's brief as INPUT rather than as a rejection later.

## Invariants

* A tracker carries EXACTLY ONE `CHANGE-NOTES` marker. A second copy switches
  auto-resolution off silently, which is why prose may name the marker but must
  never write it out again.
* A branch's section is everything its base carried, followed by that branch's
  own additions — checked as a literal prefix of both sides.
* `lead` decides whose lines come first, and it is not cosmetic: merging a task
  INTO the base leads with the base's lines, refreshing a task FROM the base
  leads with the incoming ones. Getting the second wrong produces a branch that
  can never be merged out again.
* An added line may not be a heading, a conflict marker or another marker, and
  the added text must end in a newline — a partial last line is the
  grow-an-existing-line shape the whole design exists to end.

## Data flow

Git conflicts on the tracker; the driver reads stages 1/2/3 from the INDEX (the
authoritative three sides, which is why nothing here parses conflict markers to
work out who changed what) plus git's own half-merged working file for the
region before the marker. The resolver splits each side at the marker, checks
the prefix rule, extracts each side's additions and concatenates them in `lead`
order. Anything it will not decide returns `None` and the merge stops.

## Tests and decisions

* `autoloop/tests/test_docs_merge.py` merges real branches through the
  production path, holds each shipped tracker to having exactly one append-only
  section, and enforces the line-length limit against the files themselves.
* `autoloop/tests/test_base_refresh_notes.py` is the round trip that catches a
  refreshed task branch which can no longer be merged back out.
* `.gitattributes` records the decision NOT to use `merge=union`: it was tried,
  measured and removed the same day, for two reasons stated there. That file is
  kept rule-free so the next person reaching for it finds out why.

## Known failure modes

* A note that EDITS, reorders or grows an existing line fails the prefix test.
  Auto-resolution then refuses and the merge stops exactly like a source
  conflict — the same outcome as a real disagreement, which is why the authoring
  rule is stated to every agent up front.
* A note longer than `MAX_NOTE_LINE_CHARS` is NOT refused by the resolver, on
  purpose: it is a documentation-shape problem, and enforcing it during a merge
  would turn a style violation into a halted sweep. It fails in the test suite
  instead.
* A section whose last line has no trailing newline makes an append continue
  that line; the resolver refuses rather than guessing.
