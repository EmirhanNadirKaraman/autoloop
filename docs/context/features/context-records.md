---
id: ctx-feature-context-records
kind: feature
status: active
summary: This format: one validator, one stamping path, and what a record may not be.
source_paths: autoloop/context_records.py docs/context/index.md autoloop/tasks.py autoloop/git_gateway.py
test_paths: autoloop/tests/test_context_records.py
task_ids: ctx-02
last_verified_commit: UNSTAMPED
superseded_by:
---

## Intent and boundaries

A project-context record is a file a program can resolve, verify and expire. The
prose about this repository already existed; what did not exist was anything
checkable — `docs/SUMMARY.md` has no ids and no status, `docs/AUTOLOOP.md` has
dates but no status, `docs/COMMON_ERRORS.md` has no ids. `docs/SECURITY.md` is
the one that works, and this format copies its discipline: a stable id, an
explicit status, evidence, and a check a reader re-runs.

In scope: the record contract, the loader, and the stamping path. NOT in scope,
deliberately: any consumer. No packet, prompt, task field or resolution step
reads a record today. A format nobody can validate is the thing being replaced,
so a consumer arrives after the contract holds, not with it.

A record POINTS. It carries no detail that belongs in a tracker, and it is not a
conversation, a round summary or an implementation write-up.

## Entry points

* `context_records.load_context_records(root, git=None)` — THE loader. Every
  `.md` file under `docs/context/` is either a parsed record or one of the
  structural names (`index.md`, `README.md`), which are themselves refused if
  they open with the record fence. Anything else is named and refused. It is
  also where a stamped record's commit is put to git.
* `context_records.parse_record(text, path)` — one record: metadata block,
  field validation, per-kind sections.
* `context_records.stamp_records(root, git=None)` — resolves HEAD through
  `GitGateway` and writes it into every record still reading `UNSTAMPED`.
* `python3 -m autoloop.context_records [check|stamp] [root]` — the re-runnable
  operator entry point. Exit 0 is a pass, 1 a refusal with the reason, 2 usage.

## Invariants

* **One validator.** Nothing loads a record another way, and the loader never
  silently skips a file.
* **One path rule.** `source_paths` and `test_paths` are validated by calling
  `tasks._validate_approved_path` itself, not a copy: a record can never name a
  path shape the task registry would refuse, and two validators cannot drift.
  The commit shape is `tasks._COMMIT_SHA_RE` for the same reason.
* **Absent input is a refusal.** A missing `docs/context/` directory and a
  missing index each raise. "Validated zero records" is never a pass.
* `status: superseded` MUST name a successor, the successor MUST resolve to a
  record in the tree, and a chain of them may not be a cycle.
* Every record names at least one source path; a `feature` names at least one
  test path; an `incident` and a `lesson` name the task ids they are evidence
  from. A record pointing nowhere cannot be verified.
* **`last_verified_commit` is a measurement.** It is `UNSTAMPED` or a full sha
  git resolves. The shape check is not the check: the loader asks `GitGateway`
  about every non-sentinel value and refuses the record by name when the object
  database does not hold it (`unknown_commit`), when it holds something that is
  not a commit, or when git could not answer at all (`unresolvable_commit`).
  That last direction is chosen against `cli._candidate_is_retired`'s and
  `orchestrator._commit_presence`'s, on purpose: there an unanswered question
  withholds a DESTRUCTIVE act, so silence is safe; here it would withhold a
  REFUSAL, and accepting a stamp because the repository was unreadable would
  pass exactly when nothing could check it.
* **A stamp is verified against the commit it names.** Every pointer a pending
  record carries must be in the TREE of the HEAD being written, enumerated with
  `ls-tree -r` through the gateway — not merely present in the checkout, which
  is equally true of an untracked file, one staged and never committed, and one
  deleted in HEAD and restored on disk. Directories are matched too, because
  `ls-tree -r` lists only blobs while an approved path may end in `/`.
* Stamping verifies every pointer BEFORE it writes, verifies the HEAD it read
  like any other commit, and touches only records still on the sentinel — which
  is what makes it re-runnable and keeps a stamp from asserting a verification
  nobody performed. Every refusal it can raise happens before the first write.

## Data flow

`load_context_records` walks `docs/context/`, parses each record, then applies
the checks that need more than one file — unique ids, successor resolution, an
index that lists every record by id and by path — and last, the one that needs
git: every stamped record's commit. Cheapest first, so a tree that fails on
shape never reaches a subprocess, and a tree with nothing stamped never builds a
gateway. `stamp_records` loads that tree first (a malformed record, or one
stamped to a commit nothing resolves, stops the run before anything is written),
reads HEAD through the gateway and resolves it — keeping the tree id the commit
object already gave it rather than spending a second `rev-parse` on a question
whose failure mode it just settled — lists that tree once, checks every pending
record's pointers against it, re-reads every pending file to confirm the
sentinel is still there, rewrites the single metadata line the parser located,
and re-loads the tree to confirm what it wrote.

## Tests and decisions

* `autoloop/tests/test_context_records.py` pins each named refusal, the
  delegation to the registry's path validator, the seeded records' pointers, the
  stamped-or-unstamped state of every record against a real HEAD, and stamping's
  idempotence.
* `tasks.TRACKER_PATHS` is UNCHANGED. Adding `docs/context/` to it would widen
  the write scope of every task in the registry at once — what `docs/SECURITY.md`
  S31 refused for `[repo].tracker_paths`.
* Records live in their own directory rather than as rows in an existing
  tracker: these files are parsed, and mixing them into prose would make every
  prose edit a parse risk.

## Known failure modes

* **A hand-typed sha.** No agent in this loop can read HEAD —
  `implement_executor.WRITE_ALLOWED_TOOLS` is Read/Grep/Glob/Edit/Write and the
  prompt carries no sha — so a sha appearing in a record an agent wrote is a
  fabricated measurement. The shape check cannot tell the two apart, so the
  loader asks git: a typed value that resolves to nothing is refused by name
  before anything reads the record, and one that happens to name a real commit
  is a review question, which is why a hand-typed sha is refused in review too.
* **A pointer that exists only in the worktree.** A record written alongside the
  code it describes names paths that are on disk and not yet in any commit, so
  stamping it before that code is committed is refused (`unverifiable_record`)
  rather than recorded as evidence HEAD does not hold. Write the record, commit
  it with its subject, then stamp — which is the order the seeds in this tree
  ship in, all on the sentinel.
* **A stale stamp.** A record stays stamped to the commit it was verified at
  even as HEAD moves; that is the honest reading, but it means a stamp alone
  does not say the record is current. `check` reports moved pointers, and
  re-verifying is an edit: put the sentinel back and stamp again.
* **A record hidden as navigation.** `README.md` and `index.md` are not parsed
  as records, so they are checked for the record fence instead — otherwise
  renaming a broken record would move it into the category nothing validates.
* **A record hidden as a dotfile.** A leading dot is the one thing the contract
  does not reach, because a record is a `.md` file the index lists and an
  editor's droppings are not records. The loader RETURNS those names rather than
  dropping them (`ContextRepository.ignored`) and `check` prints each one, so a
  file under this tree that nothing validated is still said out loud. It is a
  report and not a refusal: `.DS_Store` is not a broken record.
* **A vacuous record.** Required sections and non-empty pointers are what stop a
  placeholder from passing; they cannot stop a record whose prose is wrong, and
  no automated check will.
