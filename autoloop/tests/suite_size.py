"""How many test files this suite has — one number, written by hand.

Two tests assert this against a real walk of the checkout: the docs-only
selection in `test_prose_doc_selection.py` and the before/after pair in
`test_test_selection.py`. Both use it as the DENOMINATOR of a narrowing ratio,
and a ratio whose bottom half drifted would read as a narrowing that never
happened.

WHY IT IS NOT DERIVED FROM THE FILESYSTEM, which is one line of `glob`. The
number is hardcoded so that adding a test file BREAKS something, and the author
who fixes it has to say — in the ledger below — what their new file does to the
selection rules: does it read one of the documents every task changes, and does
it start an interpreter. Deriving it would silently delete a check this
repository intends to have, and the ledger is where that check is paid: every
bump carries the reasoning for its own file.

WHY ONE HOME (conc-14, 2026-09-10). It was declared twice, once per test file,
which is the shape this repository's own guide warns about for
`note_merge.MAX_NOTE_LINE_CHARS` in words that applied here verbatim — "a second
copy agrees today and silently disagrees the first time it moves". Worse than
disagreeing: two concurrent tasks that each add a test file conflicted in BOTH
copies, and every conflicted path has to be resolvable or a carry-forward
resolves nothing at all. One home is one conflicted file.

THE SHAPE OF THIS FILE IS LOAD-BEARING, not a layout preference.
`note_merge.resolve_counter_bump` combines two branches that each bumped this
counter for their own new file, and it can only do that if it can tell the
ledger from everything else:

  * the marker comment below appears exactly ONCE;
  * everything between it and the last line is comment or blank lines;
  * the LAST line of the file is the counter assignment and nothing follows it.

`test_base_refresh_notes.py::test_the_shipped_counter_file_has_the_shape_the_
resolver_requires` checks this file against that rule, so a reformat that
switches the resolution off fails there rather than by parking a merge weeks
later. `::test_the_counter_is_assigned_in_exactly_one_file` keeps the single
home from quietly becoming two again.

Deliberately imports nothing and resolves no `__file__`: a module that can
address the checkout it lives in and names a document becomes a READER of that
document under the selection rule, which would move the very numbers this file
exists to hold still. Every document named below is named in a comment, which
`ast` does not see.
"""

# SUITE-SIZE-NOTES: append ONE new line per test file added, at the END of the
# ledger, then bump the counter on the last line of this file. Never grow,
# edit or reorder a line that is already here, and put nothing after the
# counter — both rules are what let two branches' bumps be combined instead of
# stopping a merge.
#
# --- moved here verbatim from `test_test_selection.py` (conc-14) -------------
#: 102 -> 103 when wanted-01 added `test_wanted_decision.py` (2026-09-01). The
#: DENOMINATOR only: the new file reads no tracker and spawns no interpreter, so
#: neither the docs-only selection nor the floor moved with it.
#: 103 -> 104 when prov-01 added `test_codex_stdout_verdict.py` (2026-09-01),
#: for the same reason and with the same effect — 20 and 24 are unchanged.
#: 104 -> 105 when prov-02 added `test_codex_preflight.py` (2026-09-01). The
#: DENOMINATOR again: it fakes the invocation boundary rather than spawning
#: one, and reads no tracker, so 20 and 24 are unchanged once more.
#: 105 -> 106 when conc-02 added `test_config_concurrency.py` (2026-09-01),
#: the DENOMINATOR again: it validates `[concurrency]` from `tmp_path` config
#: strings and reads no tracker, so 20 and 24 are unchanged once more.
#: 106 -> 107 when conc-05 added `test_lane_state.py` (2026-09-01), the
#: DENOMINATOR again: it resolves lane paths and lease records under
#: `tmp_path`, reads no tracker and spawns nothing, so 20 and 24 are unchanged
#: once more.
#: 107 -> 108 when conc-06 added `test_fleet_supervisor.py` (2026-09-02), the
#: DENOMINATOR again: it plans against in-memory registries, reaches its
#: trackers through `tasks.TRACKER_PATHS` instead of naming one, resolves no
#: `__file__` and spawns nothing, so 20 and 24 are unchanged once more.
#: 108 -> 109 when conc-11 added `test_fleet_throttle.py` (2026-09-02), the
#: DENOMINATOR again: it works on one small JSON record and two orchestrators
#: under `tmp_path`, names no tracker, resolves no `__file__`, and its only
#: concurrency is `threading` — which is not a spawn entry point under the rule
#: above — so 20 and 24 are unchanged once more.
#: 109 -> 110 when conc-03b added `test_merge_rereview.py` (2026-09-03), the
#: DENOMINATOR again: it names no tracker, resolves no `__file__`, and its
#: `run_git` hands an unreadable argv to `subprocess.run` with no interpreter
#: literal anywhere in the file — so neither term of the opacity rule above
#: holds and 20 and 24 are unchanged once more.
#: 110 -> 111 when conc-04b added `test_lane_observed_checkout.py` (2026-09-03),
#: the DENOMINATOR again: it names `docs/AUTOLOOP.md`, which is not one of the
#: change-note trackers a docs-only round changes, resolves no `__file__`, and
#: borrows `gitrepo.run_git` without an interpreter literal of its own — so 20
#: and 24 are unchanged once more.
#: 111 -> 112 when ctx-03 added `test_context_resolver.py` (2026-09-03), the
#: DENOMINATOR again: the one document it names is `autoloop/config.example.toml`
#: (through its own `__file__`, exactly as `test_config_concurrency.py` does),
#: which is not one of the change-note trackers a docs-only round changes, and
#: its `CountingRunner` hands `subprocess.run` an argv this cannot read with no
#: interpreter literal anywhere in the file — so 20 and 24 are unchanged once
#: more.
#: 112 -> 113 when conc-07 added `test_fault_isolation.py` (2026-09-03), the
#: DENOMINATOR again: the one document it names is `docs/AUTOLOOP.md`, which is
#: not one of the change-note trackers a docs-only round changes, it resolves no
#: `__file__`, and it spawns no process at all — so 20 and 24 are unchanged once
#: more.
#: 113 -> 114 when conc-08 added `test_lane_death_recovery.py` (2026-09-03), the
#: DENOMINATOR once again and for conc-07's reason exactly: the one document it
#: names is `docs/AUTOLOOP.md`, it resolves no `__file__`, and the git it needs
#: is spawned by `gitrepo.py` rather than by anything this file binds — so 20 and
#: 24 hold.
#: 114 -> 115 when conc-10 added `test_fleet_end_to_end.py` (2026-09-08), the
#: DENOMINATOR again and for conc-07's and conc-08's reason: the one document it
#: names is `docs/AUTOLOOP.md`, it resolves no `__file__`, and its only
#: concurrency is `threading` — no repository, no subprocess and no interpreter
#: literal anywhere in it — so 20 and 24 hold.
#: 115 -> 116 when ctx-05 added `test_context_packet.py` (2026-09-09), the
#: DENOMINATOR again. It spells `CLAUDE.md` in an evaluated string — the scope
#: line a context packet renders unions the trackers in — and is still not a
#: reader of it, because the rule is a CONJUNCTION and this file resolves no
#: `__file__`. It spawns no interpreter either (its git comes from
#: `gitrepo.py`), so 20 and 24 hold.
#: 116 -> 117 when conc-12 added `test_lane_hold_scheduling.py` (2026-09-09), the
#: DENOMINATOR again and for conc-10's reason: it names no document at all, it
#: resolves no `__file__`, and it builds no repository and spawns no process —
#: its only git is a three-method stub — so 20 and 24 hold.
#: 116 -> 117 when ctx-07 added `test_context_closeout.py` (2026-09-09), the
#: DENOMINATOR again. It names no change-note tracker in any evaluated string —
#: the one it spells is `TRACKER_PATHS`, the identifier, which holds paths rather
#: than being one — resolves no `__file__` of its own (the source it reads is
#: reached as an ATTRIBUTE, `sys.modules[...].__file__`, which is not an
#: `ast.Name`), and its git comes from `gitrepo.py`, so 20 and 24 hold.
#: 118 -> 119 when ctx-08 added `test_context_diagnostics.py` (2026-09-09), the
#: DENOMINATOR again and for the same reason: it names no change-note tracker in
#: any evaluated string, resolves no `__file__`, and its git comes from
#: `gitrepo.py`, so 20 and 24 hold.
#: 119 -> 120 when split-06 added `test_split_order_advisory.py` (2026-09-10),
#: the DENOMINATOR again and for ctx-08's reason: it resolves no `__file__`, so
#: the `.md` extension token it does spell attributes it no document; it spawns
#: nothing of its own (its git comes from `test_task_split.py`, whose own
#: opacity is unchanged by being imported), so 20 and 24 hold.
#
# --- moved here verbatim from `test_prose_doc_selection.py` (conc-14) --------
#: 102 -> 103 when wanted-01 added `test_wanted_decision.py` (2026-09-01). Only
#: the DENOMINATOR moved: the new file reads no tracker and spawns nothing, so a
#: docs-only round still selects the same 20.
#: 103 -> 104 when prov-01 added `test_codex_stdout_verdict.py` (2026-09-01),
#: for the same reason and with the same effect: still the same 20.
#: 104 -> 105 when prov-02 added `test_codex_preflight.py` (2026-09-01): it
#: fakes the invocation boundary and reads no tracker, so still the same 20.
#: 105 -> 106 when conc-02 added `test_config_concurrency.py` (2026-09-01):
#: it loads configs from `tmp_path` strings and reads no tracker, so the same
#: 20 once more — the DENOMINATOR only.
#: 106 -> 107 when conc-05 added `test_lane_state.py` (2026-09-01): it works
#: on lane paths and lease records under `tmp_path` and reads no tracker, so
#: the same 20 once more — the DENOMINATOR only.
#: 107 -> 108 when conc-06 added `test_fleet_supervisor.py` (2026-09-02): it
#: plans against registries built in memory, names its trackers through
#: `tasks.TRACKER_PATHS` rather than by spelling them, and resolves no
#: `__file__` — so it reads no document under either half of the rule above and
#: the same 20 hold. The DENOMINATOR only.
#: 108 -> 109 when conc-11 added `test_fleet_throttle.py` (2026-09-02): it works
#: on one small JSON record and two orchestrators under `tmp_path`, names its
#: trackers not at all, resolves no `__file__`, and its only concurrency is
#: `threading` — which is not a spawn under the opacity rule — so the same 20
#: once more. The DENOMINATOR only.
#: 109 -> 110 when conc-03b added `test_merge_rereview.py` (2026-09-03): it
#: names no tracker, resolves no `__file__`, and its `run_git` hands an argv
#: this cannot read to `subprocess.run` WITHOUT any interpreter literal in the
#: file — `opaque` is `starts_an_interpreter or (interpreter_seen and
#: unreadable)`, and both terms are False — so the same 20 once more. The
#: DENOMINATOR only.
#: 110 -> 111 when conc-04b added `test_lane_observed_checkout.py` (2026-09-03):
#: the one document it names is `docs/AUTOLOOP.md`, which is not a change-note
#: tracker, it resolves no `__file__`, and it borrows `gitrepo.run_git` with no
#: interpreter literal of its own — so the same 20 once more. The DENOMINATOR
#: only.
#: 111 -> 112 when ctx-03 added `test_context_resolver.py` (2026-09-03): it does
#: resolve its own `__file__`, and the one document it names is
#: `autoloop/config.example.toml` — not a change-note tracker, so a docs-only
#: round still reaches it through neither half of the rule — and its
#: `CountingRunner` hands `subprocess.run` an unreadable argv with no interpreter
#: literal in the file. The same 20 once more; the DENOMINATOR only.
#: 112 -> 113 when conc-07 added `test_fault_isolation.py` (2026-09-03): the one
#: document it names is `docs/AUTOLOOP.md`, which is not a change-note tracker,
#: it resolves no `__file__`, and it spawns nothing at all — so it reads no
#: document under either half of the rule and the same 20 hold. The DENOMINATOR
#: only.
#: 113 -> 114 when conc-08 added `test_lane_death_recovery.py` (2026-09-03), for
#: conc-07's reason exactly: `docs/AUTOLOOP.md` is the only document it names, it
#: resolves no `__file__`, and the git repositories it builds are spawned by
#: `gitrepo.py`, not by anything this file binds. The same 20 hold; the
#: DENOMINATOR only.
#: 114 -> 115 when conc-10 added `test_fleet_end_to_end.py` (2026-09-08), again
#: for conc-07's reason: `docs/AUTOLOOP.md` is the only document it names, it
#: resolves no `__file__`, and it builds no repository and spawns nothing — its
#: only concurrency is `threading`. The same 20 hold; the DENOMINATOR only.
#: 115 -> 116 when ctx-05 added `test_context_packet.py` (2026-09-09), the
#: DENOMINATOR again. It DOES spell `CLAUDE.md` in an evaluated string — the
#: scope line a context packet renders unions the trackers in — but the reader
#: rule is a CONJUNCTION: it resolves no `__file__`, so it cannot address the
#: checkout it lives in and is not a reader of that tracker. It spawns nothing
#: either (its git comes from `gitrepo.py`), so it is not on the opaque frontier
#: and the same 20 hold.
#: 116 -> 117 when conc-12 added `test_lane_hold_scheduling.py` (2026-09-09), the
#: DENOMINATOR again and for conc-10's reason: it names no document at all, it
#: resolves no `__file__`, and it builds no repository and spawns nothing — its
#: only git is a three-method stub — so it is neither a reader nor on the opaque
#: frontier and the same 20 hold.
#: 116 -> 117 when ctx-07 added `test_context_closeout.py` (2026-09-09), the
#: DENOMINATOR again, and this one does not even reach ctx-05's conjunction: it
#: spells no change-note tracker in any evaluated string (`TRACKER_PATHS` is the
#: identifier, not a path), and the module source it reads is reached as the
#: ATTRIBUTE `sys.modules[...].__file__`, which is not the `ast.Name` the rule
#: looks for. It spawns nothing of its own either, so the same 20 hold.
#: 118 -> 119 when ctx-08 added `test_context_diagnostics.py` (2026-09-09), the
#: DENOMINATOR again and for ctx-07's reason: it names no change-note tracker in
#: any evaluated string, resolves no `__file__`, and spawns nothing of its own
#: (its git comes from `gitrepo.py`), so it is neither a reader nor on the opaque
#: frontier and the same 20 hold.
#: 119 -> 120 when split-06 added `test_split_order_advisory.py` (2026-09-10),
#: the DENOMINATOR again and for ctx-08's reason: it resolves no `__file__`, so
#: the `.md` extension token it does spell attributes it no document, and it
#: spawns nothing of its own — its git comes from `test_task_split.py`, and
#: importing an opaque file does not make the importer opaque — so the same 20
#: hold.
#
# --- one home from here on (conc-14, 2026-09-10) -----------------------------
# Append below this line. One line per test file added, saying what the new file
# does to BOTH selections — the docs-only 20 in `test_prose_doc_selection.py`
# and the 20/24 pair in `test_test_selection.py` — and then bump the counter.
#: 122 -> 123 when ctx-16 added `test_config_context_records.py` (2026-09-11),
#: the DENOMINATOR only. It DOES resolve its own `__file__` — it loads
#: `autoloop/config.example.toml`, exactly as `test_config_concurrency.py` and
#: `test_context_resolver.py` do — but the reader rule is a CONJUNCTION over
#: change-note trackers, and the only documents it spells are that example file
#: and the repository-relative `docs/context` a record store points at, neither
#: of which is one. It spawns nothing at all (no repository, no subprocess, no
#: interpreter literal), so it is not on the opaque frontier either: the
#: docs-only 20 and the 20/24 pair both hold.
#: 123 -> 124 when ctx-14 added `test_context_record_checks.py` (2026-09-11),
#: the DENOMINATOR only. It names no change-note tracker in any evaluated string
#: — the one document-shaped string it spells is the repository-relative
#: `docs/context` a record store points at, which is not one — resolves no
#: `__file__`, and its git comes from `gitrepo.py` with no interpreter literal
#: anywhere in the file, so it is neither a reader nor on the opaque frontier:
#: the docs-only 20 and the 20/24 pair both hold.
#: 120 -> 121 when dash-08 added `test_action_log_panel.py` (2026-09-11), the
#: DENOMINATOR only, for both selections: it names no change-note tracker in any
#: evaluated string and resolves no `__file__`, so it reads no document under the
#: conjunction; and its one spawn is `subprocess.run([node, path])` where `node`
#: is `shutil.which("node")` — an argv the scan cannot read, but with no
#: `python`/`python3` literal and no `sys.executable` anywhere in the file, so
#: neither term of the opacity rule holds (its git comes from `gitrepo.py`). The
#: docs-only 20 and the 20/24 pair hold.
SUITE_SIZE = 125
