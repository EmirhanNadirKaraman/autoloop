---
id: ctx-architecture-loop-spine
kind: architecture
status: active
summary: The components one round passes through, and the boundary each one owns.
source_paths: autoloop/orchestrator.py autoloop/tasks.py autoloop/git_gateway.py autoloop/policy.py autoloop/validation.py autoloop/implement_executor.py
test_paths: autoloop/tests/test_orchestrator.py autoloop/tests/test_git_gateway.py autoloop/tests/test_policy.py autoloop/tests/test_test_selection.py
task_ids: ctx-02
last_verified_commit: UNSTAMPED
superseded_by:
---

## The spine

`autoloop/orchestrator.py` is the loop: phases, dispatch, review, merge,
self-upgrade. Every other module below is a boundary it calls through rather
than a layer it sits on, and the reason each boundary exists is that the
orchestrator must not be able to do that thing directly.

| Boundary | Module | What it owns |
|---|---|---|
| The roadmap | `autoloop/tasks.py` | Stable task ids, the dependency graph, the approved paths a round may write. Does no git of its own, by design. |
| Every git call | `autoloop/git_gateway.py` | Subprocess git with an argument list and an explicit cwd; no `shell=True`, no ambient push. |
| What git may be asked | `autoloop/policy.py` | The command whitelist every gateway call is validated against first, read-only calls included. |
| What is run to prove a change | `autoloop/validation.py` | The validation commands, and which test files a commit's changed paths can reach. |
| The write-capable agent | `autoloop/implement_executor.py` | The agent's tools and its scope. `WRITE_ALLOWED_TOOLS` is Read/Grep/Glob/Edit/Write — no shell, so no agent in this harness can run git or read HEAD. |

## What holds them apart

* **Scope is decided in one place.** `tasks.effective_approved_paths` is a
  task's declared paths UNION the documentation trackers, and
  `tasks.unauthorized_paths` is the single matcher; the deletion path
  (`tasks.deletable_paths`) reuses both rather than restating the rule, so a
  deletion is refused by exactly the code that records an out-of-scope write.
* **Evidence is re-checked, never trusted once.** A task recorded as shipped
  under other commits carries the commits (`TaskRegistry.record_shipped_elsewhere`),
  and every reader with a checkout re-checks their ancestry.
* **A refusal must be distinguishable from an absent answer.**
  `GitGateway.object_exists` answers True/False only when git actually
  answered, and raises on anything else, so "the object is missing" can never be
  confused with "git could not look".

## Where the detail lives

`docs/SUMMARY.md` for what each file is for, `docs/AUTOLOOP.md` for how a
mechanism behaves and when it changed, `docs/SECURITY.md` for the findings that
shaped these boundaries. This record names the boundaries and points at them; it
is not a design document and must not become one.
