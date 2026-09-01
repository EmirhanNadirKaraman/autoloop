# Context records

Machine-validated project context. Every file in this directory loads through
`autoloop/context_records.py`, which refuses a malformed record by name and
never skips one. From the repository root:

```
python3 -m autoloop.context_records check
python3 -m autoloop.context_records stamp
```

`check` validates the whole tree and reports any record whose source or test
paths have moved. `stamp` does the same and then writes this repository's HEAD
into every record still carrying the `UNSTAMPED` sentinel, resolving it through
`GitGateway` — the way every other component in the package asks git.

**`last_verified_commit` is never typed by hand.** A write-capable agent in this
loop has no shell and is handed no sha, so a sha in a record an agent wrote
would be a fabricated measurement. `UNSTAMPED` is a stated gap, not a
placeholder: it says no run has yet checked this record against a commit. Any
other value is put to git on every load and the record is refused by name unless
this repository resolves it to a commit — including when git cannot be asked at
all, because a check that passes when nothing could answer is not a check.

A record POINTS at the document that carries the detail — `docs/SUMMARY.md`
(what each file is for), `docs/AUTOLOOP.md` (how a mechanism behaves),
`docs/SECURITY.md` (findings, with ids and status), `docs/COMMON_ERRORS.md`
(symptoms) — and never restates it.

## Records

| id | kind | status | file | summary |
|---|---|---|---|---|
| ctx-project-autoloop | project | active | `project.md` | What this repository is, and which document answers which question about it. |
| ctx-architecture-loop-spine | architecture | active | `architecture.md` | The components one round passes through, and the boundary each one owns. |
| ctx-feature-append-only-change-notes | feature | active | `features/append-only-change-notes.md` | Why every tracker ends in an append-only ledger, and what makes two branches' notes merge without a human. |
| ctx-feature-context-records | feature | active | `features/context-records.md` | This format: one validator, one stamping path, and what a record may not be. |
| ctx-incident-rate-limit-as-browser-fault | incident | resolved | `incidents/rate-limit-read-as-a-browser-fault.md` | An account-level throttle was reported as a lost browser session and burned a task's attempt budget overnight. |
| ctx-decision-tracker-paths-not-configurable | decision | active | `decisions/tracker-paths-are-not-configurable.md` | The always-writable documentation paths are a reviewed constant, not a config key. |
| ctx-lesson-absent-evidence-is-not-a-pass | lesson | active | `lessons/absent-evidence-is-not-a-pass.md` | A recheck that filters evidence by name must require the evidence to be present; absence has twice read as health. |

## Adding one

1. Pick the kind. `feature`, `incident` and `lesson` have required sections —
   `context_records.REQUIRED_SECTIONS` is the list, and a record missing one is
   refused with the heading named.
2. A `lesson` exists only when the same mistake has happened MORE THAN ONCE, a
   reviewer named a reusable failure pattern, or the mistake exposed a
   non-obvious project invariant. A one-off is not project context.
3. Name real paths. Every entry in `source_paths` and `test_paths` is validated
   by the task registry's own `_validate_approved_path`, and `check` reports any
   that no longer exist.
4. Add the row above. A record the index does not list, by id and by path, is
   refused.
5. Leave `last_verified_commit: UNSTAMPED` and let `stamp` write it.
