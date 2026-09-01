---
id: ctx-decision-tracker-paths-not-configurable
kind: decision
status: active
summary: The always-writable documentation paths are a reviewed constant, not a config key.
source_paths: autoloop/tasks.py autoloop/config.py autoloop/orchestrator.py autoloop/config.example.toml docs/SECURITY.md
test_paths: autoloop/tests/test_tasks.py autoloop/tests/test_config_repo_section.py autoloop/tests/test_scoped_delete.py
task_ids: port-02
last_verified_commit: UNSTAMPED
superseded_by:
---

## The decision

`tasks.TRACKER_PATHS` — the documentation files EVERY task may write so it can
append its own change note — is a constant in reviewed source. It is not read
from the loop's runtime configuration, and `[repo]` has no key that can supply
one.

## Why

Widening the write scope of every task at once must be a diff someone reviews.
`.autoloop/config.toml` lives under the gitignored state directory, so an edit
to it appears in no repository history: a config key would let one unreviewed
line grant every future round write access to a new path.

The alternative was built and withdrawn before shipping (2026-08-16, port-02):
`[repo].tracker_paths`, bounded by a filename-suffix blocklist meaning "nothing
that looks like code or configuration". The blocklist cannot carry that weight —
`.env`, `.gitignore`, `Makefile`, `Dockerfile`, `Gemfile` and any extensionless
script are all behaviour-changing files with no refused suffix, and that set is
open-ended, so extending the list is not a fix. It swapped a hard control for an
unenforceable heuristic.

## How another repository declares its own

By editing the constant. That is not a workaround: `autoloop/` is VENDORED into
the repository it operates on, so the constant already is per-repository
metadata living in git-tracked source, and changing it in a target repo is a
commit in that repo's reviewed history — the property a config edit lacks. The
recorded forward path, if a declaration in repository DATA is ever wanted, is a
COMMITTED file at the repository root; the requirement it must meet is that
changing it appears in the history, not merely that it is per-repository.

## Consequences, stated rather than hidden

* `docs/SECURITY.md` is in the list, so an agent can edit the record of a
  finding without that being named in its task. It is documentation, not a
  control — but a weakened finding can mislead a later reader, and it is why the
  list stays short.
* `CLAUDE.md` is in the list, so an executor can edit the instructions later
  agents read. Three things bound that: the file changes no runtime behaviour,
  `approved_paths` is enforced from the `Task` and never from anything an agent
  writes, and every edit stays visible in `commit_range_paths`.
* A tracker may be WRITTEN by every task and DELETED by none:
  `tasks.deletable_paths` refuses one unconditionally and reports the refusal in
  its own category, because a grant that exists for appending is not a licence
  to remove someone else's note.
* `docs/context/` was NOT added to the list when this record was written
  (ctx-02). Records are edited by the tasks that own them, under their own
  approved paths.

## Where the detail lives

`autoloop/tasks.py`, the comment block above `TRACKER_PATHS`, is the primary
record and carries the full argument. Note a drift worth knowing before you go
looking: several modules cite "`docs/SECURITY.md` S31" for this decision, and
this repository's `docs/SECURITY.md` does not carry an S31 entry — its open
findings section reads "None recorded yet in this repository", because the
finding list was not part of what the extraction brought over. The citations
name a document section that exists in the parent repository, not here.
