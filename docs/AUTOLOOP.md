# AUTOLOOP.md

The operator's guide to running autoloop.

**About this file.** Modules across this package cite `docs/AUTOLOOP.md` by
numbered section (§3h, §4h, §5d, §9c, §11 …). Those sections belong to the guide
as it stood in the `language-app` repository this package was extracted from on
2026-08-27 with `git filter-repo --path autoloop/`, which carried the package and
not that document. The citations are left as they are — they name real
paragraphs, and rewriting them to point at nothing would lose the reference —
but the numbering is **not** reconstructed here, and nothing below invents a
section number. This file carries the operator-facing documentation written
since the split; each section is titled rather than numbered.

---

## One view across projects

Running one loop per project — separate `state_dir`, separate checkout,
separate branch, separate reviewer transport — works: `LoopLock` is scoped to a
state directory and fails closed, and worker repositories are per task, so the
projects do not collide. What does not follow from that is any way to *see* all
of them. The dashboard binds one checkout; `health` judges one config. With four
loops, noticing that one of them stopped means opening four pages or reading four
logs, and the realistic outcome is that a stopped loop sits unnoticed for hours.

    python -m autoloop projects

answers, for every configured project: **is it running, what task is it on, how
long has it been silent, does it have open blockers, and when did it last land
work.**

### Configuring it

    [projects]
    configs = [
      "~/code/project-a/.autoloop/config.toml",
      "~/code/project-b/.autoloop/config.toml",
    ]

The input is a list of **config paths**, not of repositories: every other path a
project has — its state directory, lock, transcript, blockers and task registry —
follows from one config file by the loop's own resolution rules. Adding a fifth
project is therefore a line in this section rather than a code change.

Paths must be absolute (`~` is expanded). The command is run from wherever the
operator happens to be standing, so a relative entry would name a different file
on each invocation; `paths.workers_root` is held to the same rule for the same
reason. Order is preserved — the rows come out in the order you wrote them, and
deliberately not sorted by severity: this is a page read many times a day, and a
list whose rows move around is a list you stop being able to scan. The count of
what needs attention goes at the bottom instead.

`--project PATH` (repeatable) takes the list on the command line instead. Given
at least once, `[projects].configs` is not consulted and neither is `--config`,
so the view also works from a machine that runs no loop of its own.

Listing this deployment's own config among the projects is allowed and shows it
as one more row. Nothing reads the section recursively.

### Reading a row

```
   PROJECT   LOOP     HEALTH        TASK      QUIET  BLOCKERS  LANDED
!  alpha     live     blocked       port-04   4m     1         3h
   beta      live     running       dash-05   1m     0         9h
!  gamma     stopped  not_running   halt-01   96m    0         2d
!  delta     unknown  unknown       —         —      —         —
      the loop's configuration could not be read
      /Users/me/code/delta/.autoloop/config.toml did not parse as TOML …
3 of 4 project(s) need attention; 1 STOPPED: gamma
```

* **`LOOP`** is a **fact about the lock**, never a verdict: `live` (a live
  `LoopLock` — boot-aware, and the same authority `health` uses), `paused` (no
  live lock, and the PAUSE flag is set), `stopped` (no live lock and nothing
  says that was intended), or `unknown` (the lock could not be read, or the
  config never loaded — *not* `stopped`, because no lock was read at all).
* **`HEALTH`** is `health`'s own verdict code, unchanged: `running`, `idle`,
  `paused`, `blocked`, `parked`, `failed`, `silent`, `stale_lock`,
  `not_running`, `stranded`, `merge_backlog_held`. The view adds exactly one
  word to that vocabulary — `unknown` — and it lives in `health.py` beside the
  others rather than in a second vocabulary of its own.
* **`TASK`** is the task the loop's own state names. It outlives its round, so
  when `LOOP` is not `live` it reads as "the last task this loop dispatched".
* **`QUIET`** is wall-clock minutes since the newest transcript entry. Machine
  sleep is deliberately *not* discounted here — that correction belongs to the
  verdict, and `HEALTH` carries it.
* **`BLOCKERS`** is counted from the blocker directory itself, not taken from
  the health verdict: `health` returns `stale_lock` before it ever reads
  blockers, so its own count is 0 on exactly the verdict where an operator most
  needs to know a decision is also waiting.
* **`LANDED`** is the newest `completed_at` in the task registry — when this
  loop last *finished* a task. Whether that work is also integrated into the
  branch is a different question, needs git and a checkout, and is deliberately
  not asked here; the single-project dashboard's merge report answers it.
* **`—` means unreadable, never zero.** A `0m` in `QUIET` reads as "just
  active" and a `0` in `BLOCKERS` reads as "nothing open"; either would be the
  fail-open this view exists to remove.

Exit codes mirror `health`, so the same cron or launchd wrapper works: **0** =
every project is fine, **1** = at least one needs you, **2** = nothing was
configured to look at. `--json` prints the same rows machine-readably, one
object per project with every field above, plus a boolean `stopped` — carried
explicitly so a notifier never has to re-derive "is this loop down" from
`loop_state` or, worse, from `code`. Deriving it from `code` is the 2026-08-15
false alarm below.

### blocked-while-running is not stopped

`LOOP` and `HEALTH` are separate columns because they genuinely disagree, and
conflating them has already cost something. `health` reports `blocked` whenever
**any** blocker is open — which is correct, a blocker does need a decision — but
continuous mode goes on working other tasks on that project meanwhile. On
2026-08-15 a reader treated that as "the loop is down", raised a false parked
alarm and sent a needless email. A blocked project whose loop is up is one row;
a project that is actually down is another; and the footer names the second kind
by name, only when there is one.

### Failure isolation

One unreachable or misconfigured project **must not blank the view**. Every
per-project read is wrapped: a config that will not load, a path that does not
exist, a directory where a file was expected, a corrupt blocker record, a
`health.check` that raises — each becomes a row reading `unknown` with the
reason under it, while every other project still renders.

Two rules make that honest rather than merely quiet:

* an `unknown` row **needs attention**. A loop nobody can see is not a loop that
  is fine, and the failure being guarded against is precisely a loop going
  unnoticed;
* the **facts survive the verdict**. If only `health.check` fails, the row still
  says whether the lock is live, what the loop was working and how long it has
  been quiet — those are what the operator acts on. Anything that could not be
  read is listed under the row and escalates that row to needing attention.

### Read-only, lock-free, and it writes nothing

The view takes no lock and is safe to run while any of those loops is mid-round,
exactly like `health` and like the dashboard's observation paths. It also opens
**no checkout at all**: it reads config files, the lock, the transcript tail, the
blocker directory and the task registry, and nothing else.

That is not politeness. `escape_detector` snapshots the observed checkout around
every write-capable agent call, ignored paths included, so anything that writes
into that tree mid-round is indistinguishable from an agent writing where it may
not — a loop-fatal `checkout_escape_detected` park. Four such incidents on
2026-08-15/16 were caused by exactly one thing: a reader importing the
checkout's own vendored package and leaving `__pycache__` inside it. Each cost a
reset and the in-flight round.

So this view imports nothing from an observed tree, and on top of that runs with
bytecode writing switched off for the duration — `sys.dont_write_bytecode` for
this process and `PYTHONDONTWRITEBYTECODE` for anything it spawns, both restored
afterwards. A test pins the whole property against a byte-for-byte snapshot of
every checkout and state directory, with a real lock held and the real
`health.check` running.

### Why a separate view rather than a dashboard mode

The single-project dashboard binds one checkout to `Handler.repo` and answers
"what is happening *inside* this project right now", with git subprocesses per
request — merge ancestry, shipped state, the dependency graph. Four of those
would be four times that cost for an answer none of them gives, and the
aggregation has no need of any of it: its input is a config path and its output
is one line per loop.

Keeping it a command rather than a page also puts it on the path the operator
already gets told things on — the same cron/launchd wrapper that runs `health`
and reads its exit code. A served page over the same rows is additive and
forecloses nothing; the aggregation (`dashboard.projects_status`, and
`render_projects_text` / `projects_json` over it) is deliberately separate from
its front door so that a later `--projects` mode of the dashboard renders the
same rows rather than re-deriving them.

---

## Reading a self-upgrade boundary in the transcript

A merge that touches `autoloop/` changes the code the loop is running, and a
live Python process does not notice. So `auto_merge` writes one
`pending_upgrade.json` record (`self_upgrade_pending`), the orchestrator offers
a **boundary** at the next `ready` phase with no packet in flight
(`self_upgrade_boundary`), and the process replaces itself with `os.execv` —
same pid, same lock.

**Every boundary is now followed by at least one outcome entry**, named
`self_upgrade_<outcome>`. Exactly one, except for a refused replacement:
`self_upgrade_exec` is written *before* `os.execv` — it has to be, there is no
"after" in a process that has been replaced — so an `execv` that raises leaves
the pair `exec` then `exec_failed`, in that order. If you find a boundary with
nothing after it at all, the process was running a build older than 2026-08-30;
that silence is the whole subject of this section.

| Entry | What happened | What the record does |
|---|---|---|
| `self_upgrade_exec` | replaced, `argv` in the entry | settled `execed` (one shot) |
| `self_upgrade_unapplicable` | the merge moved a different checkout, or named a `repo_root` this process cannot resolve | **stays pending** |
| `self_upgrade_preflight_failed` | the merged tree does not import | **stays pending**, `detail` carries the error |
| `self_upgrade_exec_failed` | marker unwritable, lock unarmable, or `execv` refused | **stays pending** |
| `self_upgrade_deferred` | this process may not hand off | **stays pending** |
| `self_upgrade_none` | nothing pending was left to act on, or its `base_sha` is not a usable key | left exactly as found |

**No outcome but `exec` ends the process, and no outcome but `exec` settles the
record.** Carrying on means carrying on with the code it has, which was working
a second ago; exiting is the one response that guarantees no further work
happens. A failed preflight in particular is *reported and refused*, never fatal
— replacing a working loop with a tree that cannot start is the failure that
check exists to prevent.

**Every refusal is retryable by the next process**, and that is the 2026-08-27
lesson stated as a rule: a refused handoff is a fact about the process that
refused it, not a judgement the next one inherits. One launch could not hand off
at 08:13:47 and the next exec'd the *same record* at 08:15:30. Until 2026-08-31
the three non-`deferred` outcomes wrote themselves into `status`, which took the
sha out of `pending` for good — and `_self_upgrade_due` only ever offers
`pending`, so the merged code sat on disk with nothing left to run it. They now
leave the record alone; `detail` records the outcome (`preflight_failed: rc=1
SyntaxError…`) so the state dir says why without a trip to the transcript. A
record on disk that still says `preflight_failed`, `unapplicable` or
`exec_failed` was settled by an older build: it is not offered, and clearing the
file is the way to re-arm it.

The exception is a refusal *after* the one-shot marker was written (an unarmable
lock, an `execv` that raised). Those restore `pending` explicitly, and if that
write fails the record is **removed** rather than left saying `execed` — a
delayed restart costs less than `_confirm_self_upgrade` retiring a replacement
that never happened. The entry says `REMOVED` when it happens; a plain process
start picks the merged code up.

**`deferred` is the outcome an operator acts on directly.** It means the
boundary was reached by a **single-round** `run` — `run` with no `--continuous`,
and therefore also `--retry`, `--answer`, `--resubmit` and `resume`, all of
which funnel into the same path. Such a process cannot hand off, because its own
command line manages ONE round: `--kickoff` refuses a session that now exists
and `--answer` refuses a phase that is no longer `needs_user`, so the successor
would die on a `StateError` instead of continuing the loop. Nothing about the
merged tree has been judged, so nothing is settled: the loop finishes the
session it is in, and the upgrade waits. Perform it with

    python -m autoloop start          # or: run --continuous

which reaches the same boundary and execs the same record.

Until 2026-08-30 that path did not defer — it **returned**, ending the process
mid-session with no entry at all, and `run`'s ordinary exit then published the
heartbeat `stopped`, which is deliberately not an attention status ("you stopped
it, you know"). On 2026-08-27 that took the loop down at 08:13:47 and it stayed
down until an operator ran `start` by hand at 08:15:26 — the same record exec'd
four seconds later, which is what proves the merge was fine and the *path* was
not.

**The loop cannot restart in a circle.** Three separate bounds, none of them a
timer:

1. **`execed` is a one-shot.** A record saying it is never offered again, so a
   successor that dies before completing one iteration is not retried;
   `_confirm_self_upgrade` retires the marker after one full pass under the
   merged code, and only then.
2. **Every answered boundary is declined for its `base_sha` in the process that
   answered it** — `Orchestrator.decline_self_upgrade`, carried across the
   per-iteration orchestrator rebuild by `_run_continuous`'s
   `answered_upgrades`. This is what a retryable record needs and where the
   whole spin bound now lives: in memory, per process, saying nothing about the
   next one. So a refused handoff costs at most one preflight (one subprocess,
   120s ceiling) and one entry per process, not one per round.

   The sha declined is **the one the decision acted on**, plus the one the loop
   read on its way in — *the two reads can disagree*. `_run_continuous` reads
   `pending_upgrade.json` before the boundary and
   `cli._self_upgrade_at_boundary` reads it again to decide, so a merge landing
   between them (or an operator editing the state dir) means record B is
   refused where record A was read. The decision therefore returns which record
   it acted on — `cli.UpgradeOutcome`, a `str` subclass carrying `base_sha` and
   `candidate_sha`, so it is still the outcome slug everywhere else — and both
   shas are declined. Declining the extra one costs at most a retry by the next
   process, since the record stays `pending` on disk and the decline is in
   memory; declining neither is the unbounded spin. When the decision acted on
   nothing (`none`), it returns no identity and the pre-read sha is the only
   key there is, which is why that read stays.
3. **A record with no usable `base_sha` is never offered at all.** Every bound
   above is keyed on that sha, so a record without one could not be declined and
   would be offered forever. Nothing the merger writes lacks one.

   "Usable" is one predicate, `auto_merge.upgrade_bound_sha`: a non-empty
   `str`. `UpgradeStore.load` builds the record with `PendingUpgrade(**data)`
   and coerces nothing, so a hand-edited or half-written file can put a `dict`,
   a `list`, an `int` or `null` in that field — and the loop then dies *between*
   `self_upgrade_boundary` and any outcome, which is the silence this whole
   section exists to end: `[:12]` on the exec path raises `TypeError` for a
   dict, an int or null, and `set.add`/`in` raise it for a list or a dict.
   `_self_upgrade_due` refuses to offer such a record; the two `cli` readers
   check again, because each of them makes its own **second read** of a file
   that is mutable between the two. At the boundary itself the outcome is
   `self_upgrade_none` naming the type it found, **nothing is written** (the
   malformed record is the evidence for fixing it), and the loop carries on with
   the code it has.

A later merge is a different `base_sha` and does get its own boundary — which is
also the unstick path for a tree that fails its preflight: `auto_merge`
overwrites the record when the fix merges, and the new sha is offered normally.

**The successor's command line is `python -m autoloop run --continuous`**, plus
`--config` and `--null-executor` when this process had them — rebuilt, not the
verb you typed. `os.execv` preserves the pid and the lock is never released
(that is the point), so a successor re-running `start` would read a live lock
naming itself, print "already running — nothing to do" and exit: a successful
handoff that still ends the loop. It is recorded in the `argv` field of
`self_upgrade_exec`.

---

## Refused work goes back as a revise (autonomous mode)

**Off unless you turn it on.** Everything below happens only with
`[autonomy] enabled = true`; with the section absent — which is every config file
written before it existed — each of these faults parks for you exactly as it
always has, and turning the flag back off restores that.

Seven blocker codes are the loop REFUSING what a round produced, and saying why:

| Code | The refusal |
|---|---|
| `post_commit_verification_failed` | the commit failed a post-commit check (ancestry, an empty range, a dirty worktree, validation) |
| `commit_refused` | git refused the commit before it happened |
| `review_feedback_unchanged` | the reviewer asked for the same change twice, so the executor changed nothing |
| `review_packet_build_failed` | the candidate passed review and could not be PRESENTED |
| `approved_paths_missing` | the task carries no `approved_paths`, so no write-capable round may start |
| `push_not_descendant` | the approved candidate is not a descendant of the task base |
| `push_tree_mismatch` | the approved candidate's tree is not the tree that was reviewed |

None of them is a decision. The park text already names the fault, and the
operator's whole step is relaying it to the agent that has to fix it — which is
why `review_feedback_unchanged` sat at a median of 5.92h, the longest of any
code, over 4 parks and 35.4h (measured 2026-08-24, 131 resolved records).

**A refusal is feedback, so the loop returns it as feedback.** Instead of
parking, the task is re-dispatched as a `revise` whose `feedback` is the refusal
text, verbatim, under one line saying that no reviewer wrote it. Nothing is
rolled back, discarded or pushed: a `revise` continues the same execution record,
the same worker repository and the same task branch, so the refused commit stays
exactly where it is.

**And it happens at most once.** The second occurrence of the same code for the
same task sets that task aside (`task_fatal`) — the quarantine
`run --continuous` already works past — rather than issuing another revise. Two
independent locks carry that:

* the **recurrence meter**, `Blocker.recurrences` summed across every open record
  for a (task, code). This is the bound, and it does not care what the refusal
  says — which matters, because refusal text carries commit shas and round
  numbers and therefore differs between two occurrences of the identical fault;
* the **refusal digest** on the blocker record, which recognises the SAME refusal
  even after the record it was first written on has been closed. It can only ever
  set a task aside sooner.

Three limits you already have compose on top and are unchanged: a self-issued
revise is refused if its feedback repeats the last round's, it is refused once
`policy.max_review_rounds` is reached, and it costs an attempt from
`MAX_TASK_ATTEMPTS` exactly as a reviewer's does.

### What you will see

    autonomous_recovery        action=revise, with the attempt and the budget
    autonomous_revise_dispatched   the round actually starting
    autonomous_revise_refused      a revise the loop declined to issue, with why

`same_refusal_repeated` is the guard firing. The other reasons are fall-throughs
— no task on the record, a task the registry does not hold, a task with no
approved plan, a refusal with no text, a revise already queued — and every one of
them parks with the question it always had.

**The blocker stays OPEN while the revise runs**, and that is deliberate rather
than a leak: a round completing proves the round ran, not that the refusal
cleared, so closing the record there would refund the meter and let the same
refusal be resent forever. So `python -m autoloop blockers` lists it throughout;
answer it as you would any other, or let the repeat set the task aside.

**Two sites are deliberately untouched.** `push_not_descendant` and
`push_tree_mismatch` are each raised twice — once for a task, once for an
operator's queued changeset. A changeset has no roadmap task, so there is nothing
to revise, and that arm parks exactly as it does today.

`max_recovery_attempts = 0` keeps the set-aside and issues no revise at all, if
you want the quarantine without the extra round.

---

## Running several tasks at once — the split plan

**Status: a PLAN, not a mechanism.** Nothing in this section is implemented.
It is the design contract conc-01 was asked to produce, so that the work can be
authorised as a sequence of independently reviewable candidates instead of one
change nobody can review. The loop is single-lane today and stays single-lane
until the last candidate below lands; every candidate before it ships with the
concurrency setting at `1`, where the loop behaves exactly as it does now.

The prize is measured, not assumed. Over the 126 rounds from 2026-08-22 to
2026-08-26 the executor took 71.8 of 101.3 wall-clock hours (71%); submit took
1.6h, packet build 3.1 minutes in total, reviewer wait ~0 because the codex CLI
is synchronous. A median successful round is 45.5m, of which 37.6m is the
executor. Everything that is not the executor totals under two hours across four
days, so N lanes approach a linear speedup in a way no other change on the
roadmap can. The cost is equally measured and is the reason this is a plan
rather than a switch: 46 completed tasks took 126 rounds, 2.7 rounds per task,
and a 4x multiplier multiplies **rework** 4x as well.

### What already exists, and must not be rebuilt

* **Worker repositories are already per task.** `WorkerRepoManager` creates
  `workers_root/<task_id>` — a genuinely separate `git init` repo with no
  remote, its own controlled empty hooks directory, seeded by a one-time local
  `git fetch`. Nothing about the workspace is shared between tasks today.
* **Execution records are already per task files.** `TaskExecutionStore` writes
  `executions/<task_id>.json`, and `context._in_flight_counts` loads every one
  of them rather than assuming a single record.
* **The merge window already reasons about several candidates at once.**
  `cli._merge_window_blockers` walks every execution record, names each, and
  says why it does or does not hold the window — see also
  `context._merge_window`, which renders that verdict into the review context
  as `merge_window: open | shut — … | unknown — …`.
* **The reviewer is not a constraint on the codex transport.** Every turn is a
  separate subprocess, so concurrent reviews are already possible. Nothing here
  designs around a shared conversation; a provider that genuinely has one would
  need its own answer, and none is written here for it.
* **A loop-owned observed checkout already exists.** esc-02 (2026-08-26) moved
  the tree `escape_detector` snapshots off the operator's primary checkout onto
  `worker_env.ObservedCheckout`, resolved from `[paths].observed_checkout` and
  defaulting beside `workers_root`. This is the single largest piece of the
  isolation work, and it is already done — what remains is to have one per lane
  rather than one per loop.

### The filed obstacle #1 has moved, and the plan says why

conc-01's brief states that the escape detector breaks under concurrency
because "the orchestrator's bookkeeping for the other three lands inside each
snapshot window, every round". **That mechanism no longer applies.** port-01
(2026-08-23) moved every writable loop path — `state.json`, `tasks.json`, the
lock, the transcript, executions, blockers, the publisher repo — out of the
checkout to a sibling of `workers_root`, and `worker_env.
validate_observed_checkout` now refuses an observed tree that is nested beneath
*or contains* the state directory, `workers_root` or either publisher path. The
orchestrator's own bookkeeping cannot land inside the snapshot window because it
does not land inside the snapshotted tree at all.

**The real collision is `ObservedCheckout.synchronize`.** It is called once per
round, strictly before the "before" snapshot, and it runs `git checkout -q -B
autoloop/observed <target>` plus an `update-ref` pin per commit. With four lanes
sharing one clone, lane B synchronising to a different commit inside lane A's
window does not add bookkeeping noise — it rewrites the entire working tree, and
`diff_snapshots` reports every path in the repository. That is
`checkout_escape_detected`, loop-fatal, on the first round in which two lanes
overlap, exactly as the brief predicted but for a different reason. The
conclusion the brief draws is unchanged and is the one this plan takes: **the
fix is isolation, not an exclusion list.**

The rejected alternative, recorded so it is not re-proposed: one shared clone
kept at the latest head, with `OBSERVED_PIN_PREFIX` refs for every lane's base.
It fails for the same reason, one level up — any merge landing mid-round forces
a re-synchronisation that lands inside every other lane's window — and it would
additionally make a single unclean clone fatal to the whole fleet.

### Decision 1 — the isolation boundary is one observed checkout per lane

Each lane gets its own `ObservedCheckout` (`observed-checkout/<lane_id>`, or the
configured path with the lane id appended) and its own worker repositories,
which it already had. `escape_detector` brackets **that lane's clone** around
**that lane's** write-capable agent call. Nothing in `escape_detector.py`
changes: it names no repository, every function takes the `GitGateway` or root
it is told to work on, and the exclusion list stays empty.

`worker_env.validate_observed_checkout` must gain one rule, and the rule is the
whole claim: **a lane's observed checkout must not be nested beneath, and must
not contain, any other lane's observed checkout.** That is the same
`_is_nested` test in both directions the function already applies to the state
dir and `workers_root`, extended to siblings.

`WorkerRepoManager.create` fetches from the lane's own clone, so
`.git/FETCH_HEAD` inside a worker repo names a tree that lane's own detector
watches. This is the property esc-02 established and it is what makes the
per-lane version attributable: an agent that goes looking for "the repo" finds
its own lane's watched tree, not a sibling's.

Priced honestly: N clones of the repository and N first fetches, each bounded by
`OBSERVED_GIT_TIMEOUT_SECONDS` (900s). The fetch is from a local path and only
the first one is a full clone; later rounds fetch the commits they lack.

#### What the new boundary can still fail to catch

The four residuals `escape_detector`'s own docstring records carry over
unchanged, per lane rather than per loop:

* a **write-then-revert inside the window** — an agent that writes a file and
  restores it before the "after" snapshot is not caught here or anywhere else in
  this codebase; closing that needs a real OS sandbox (docs/SECURITY.md S24);
* **`.git/` internals** — a planted hook, a rewritten `.git/config` or a moved
  ref in the observed clone is outside the working tree the three `ls-files`
  categories enumerate, and is not covered;
* a **forged `.pyc`** whose `(mtime, size)` or PEP 552 header matches its
  source, which the interpreter would load and `is_derived_bytecode` exempts;
* **writes into the operator's primary checkout**, which nothing snapshots since
  esc-02 — the deliberate trade made there, because a tree the operator also
  writes to produces changes nothing can attribute.

And one residual that is **new under concurrency**, stated rather than papered
over: **lane A's agent writing into lane B's observed clone or worker repo.**
Lane B's window brackets that path, so the write is *detected* — but it is
attributed to B, which is precisely the half of the claim that says a write by
one lane is not attributed to another. The bound on it is the FETCH_HEAD
argument above: the only absolute path to a non-worker tree that leaks into a
worker repo names that lane's own clone, so an agent has to already know a
sibling lane's absolute path from somewhere else. That is the same out-of-scope
case esc-02 already concedes for the operator's checkout, one lane over, and it
is not claimed to be closed. What IS claimed: a lane's own bookkeeping, its own
synchronisation and its own agent can no longer be confused with another lane's,
because no two lanes share a tree.

One consequence of this residual is Decision 5's carve-out: an escape stays
fleet-fatal.

### Decision 2 — one fleet lock, N lane leases, N state files

`LoopLock` is unchanged and stays the single-holder-per-state-dir, fail-closed,
boot-aware, never-stolen lock it is today. The **fleet supervisor** holds it, and
at `lanes = 1` the supervisor *is* the loop, so the lock file, its path, its
adoption rule and `unlock`'s refusal of live locks are byte-identical to today.

Each lane gets:

* its **own state file**. Lane 0 writes literally `state.json`, at the path it
  writes today; lane *k>0* writes `lanes/<lane_id>/state.json`. That asymmetry
  is deliberate and is the N=1 criterion made structural — at `lanes = 1` no new
  file exists and no existing reader moves.
* its **own lease**, a small record beside its state file carrying pid,
  hostname, boot-relative `started_at` and a run id, judged live by exactly
  `LoopLock.is_live`'s rules (foreign host → live; predates boot → dead; pid
  probe otherwise). Reusing that predicate rather than writing a second one is
  the same argument `cli._merge_window_blockers` makes about itself: two
  implementations of "is it alive" drift, and the one that drifts is the one
  that lets two agents into one lane.

A lane's state machine is otherwise the one that exists: `Phase`, the outbox,
`PendingRequest`, `PostcommitBinding`, the packet-outstanding phases, the
failure budgets. Nothing about a single round changes. What is new is that
`current_task` and `phase` belong to a lane, and **the fleet record carries
neither** — see Decision 7.

### Decision 3 — overlapping write scopes, answered as correctness

The brief asks for this explicitly, so here is the explicit answer: **overlap is
handled by the merge protocol, not by the scheduler.** Four steps, none of which
depends on admission control:

1. A lane's agent works in its own worker repository. A write outside it is
   what Decision 1's detector *reports* — after the fact, and before anything
   is committed or reviewed. Nothing here prevents such a write; the detector's
   own docstring is explicit that it is detection, not a sandbox.
2. A candidate's diff is gated by `tasks.unauthorized_paths` against that
   task's own `tasks.effective_approved_paths` before it is committed, exactly
   as today (`implement_executor`'s pre-commit scope gate). Two lanes with
   overlapping scope cannot authorise each other's writes, because the check is
   per diff and per task.
3. The only way overlap becomes *incorrect* is a merge that lands candidate A
   and then merges candidate B on an approval taken against the pre-A base:
   B's diff would apply over text A changed, which no reviewer saw. That is
   exactly what Decision 6 forbids.
4. Textual conflict is therefore a **cost**, not an incorrectness. It surfaces
   as a rebase that will not apply, and it ends in a park that destroys nothing.

**Admission control is an efficiency measure on top of that, and its rule has to
survive the universal tracker grant.** Every task's `effective_approved_paths`
is its declared list UNION the six shared documentation trackers, so an overlap
gate computed over the effective list would find an overlap between any two
tasks and nothing would ever co-schedule. The gate is therefore computed over
**declared** `approved_paths` only, and only at **file granularity**:

* **Gates.** Two co-scheduled tasks must not both declare the same *file* entry
  (an entry not ending in `/`) outside the six universal trackers. A same-file
  declaration is the strongest advance signal that both lanes will edit the same
  file, which is the case no resolver handles.
* **Does not gate: the four append-only trackers** (`docs/SUMMARY.md`,
  `docs/TESTS.md`, `docs/SECURITY.md`, `docs/COMMON_ERRORS.md`). Every task
  appends to them by construction, and `note_merge.py` exists to resolve exactly
  that — conditional on the discipline CLAUDE.md states: one new line, appended
  at the end, no pre-existing line touched, nothing outside the section, at most
  `note_merge.MAX_NOTE_LINE_CHARS`. The resolver switches itself off silently
  when that discipline is broken, so concurrency raises the price of breaking
  it, and the enabling candidate must say so in the operator docs.
* **Does not gate: a shared directory entry** such as `autoloop/tests/`, which
  nearly every task declares. Gating on it would serialise the fleet and buy
  nothing; two tasks adding different files under it do not conflict.
* **Does not gate, and is a stated residual: `CLAUDE.md` and `docs/SCHEMA.md`.**
  They are universally granted, they have no resolver, and a conflict in either
  deliberately stops a merge because they carry claims that need a human. A task
  that *declares* one of them explicitly gates like any other file entry; a task
  that writes one it never declared is the residual. Its cost is bounded: the
  serialised merge finds the conflict, the second candidate parks, and a human
  reads two files. That is the designed behaviour of those two trackers, not a
  new failure mode.

When the gate fires the task **stays queued** — `pending` in the registry,
untouched, dispatched as soon as the conflicting lane finishes. It is never
failed, never charged an attempt, and never quarantined.

### Decision 4 — the fleet cap

`[concurrency] lanes = N`, an integer, **default 1**. `load_config` refuses `0`,
a negative, a non-integer and a value above a hard ceiling with a `ConfigError`,
in the same style as `[paths]`'s refusals — a fleet size nobody can name is not
a fleet size to guess at.

When the cap is reached, the scheduler dispatches nothing more. The registry is
the queue and stays the queue: no second data structure is introduced, the next
READY task simply is not selected this tick. The reason is recorded once per
tick in the transcript and shown by `health` and the dashboard, because a fleet
sitting at its cap and a fleet with nothing to do look identical from outside
and must not read identically.

`lanes = 1` must remain a supported configuration behaving exactly as today —
that is the brief's DEGRADE TO ONE bound, and it is how a broken lane gets
turned off rather than debugged in production.

### Decision 5 — fault isolation, and the one thing that stays fleet-fatal

Today every `loop_fatal` park stops the single loop, and `cli.py` treats a
missing or unrecognised `park_kind` as `loop_fatal` — fail-closed. The fleet
splits that vocabulary in two:

* **`lane_fatal`** — this lane stops and parks; the fleet keeps running. Its
  blocker record names the lane.
* **`fleet_fatal`** — every lane is stopped at its next safe phase.

The direction of the default does not change: an unrecognised or absent kind is
**`fleet_fatal`**. Which existing codes move to `lane_fatal` is enumerated by the
candidate that does the work, against the actual call sites, not guessed here.

**`checkout_escape_detected` stays `fleet_fatal`, deliberately.** It is
tempting to make it lane-fatal now that the boundary is per-lane and the
violation is attributable — and that is the wrong direction. The evidence an
escape produces is precisely "this agent wrote outside the boundary it was
given", and the nearest things outside that boundary are the other lanes' trees.
An agent that has demonstrated it writes where it may not is not one to keep
three neighbours running alongside. This is the fail-closed reading and it is
the same judgement `escape_detector`'s docstring makes about its own exclusion
list.

### Decision 6 — merging is serialised and rebase-aware

This is obstacle #3 and the one with a measured history: the brief records
`task_base_behind_head` as the most common blocker code in this system's
history, seventeen of them, and on 2026-08-22 three branches cut from one base
all conflicted nineteen commits later with none mergeable. Four lanes
manufacture that continuously.

**The blocking fact nobody has written down yet:** today
`cli._merge_window_blockers` shuts the window whenever *any* execution record
holds a live candidate bound to the current head — its four exemptions
(terminal registry state, already published, a retired defect, a base that is
already a proper ancestor) all describe records that are NOT the in-flight case
a lane produces. With N lanes, N−1 of them are exactly the case that blocks, so
under concurrency the merge window as written would essentially never open. The
predicate is a fleet-wide mutual exclusion, and it has to become a
per-candidate **obligation**:

> The window may open while candidates are bound to the head, provided every
> such candidate is carried forward and **re-reviewed** before it can be pushed.

The mechanics already exist and are named rather than rebuilt:

* `_carry_reviewed_candidate_past` merges the moved head INTO the task's own
  branch so a reviewed candidate survives the base moving. It is the path a
  concurrent fleet runs on. It bails on a dirty worker tree and on a merge
  conflict, and **that bail rate is the rework multiplier the brief's gating
  paragraph is about** — saying so is the point, not a caveat.
* `_rebase_execution_if_stale` refuses to re-point a record with
  `review_round > 0` and parks `task_base_behind_head`. It stays the refusal for
  the cases the carry-forward cannot handle.
* **Re-review needs no new mechanism, and this is the cheap half of the claim.**
  A carry-forward or rebase produces a new candidate commit, so
  `PostcommitBinding.candidate_sha` and `candidate_tree_sha` no longer match what
  `_dispatch_task_push` re-derives, and `packet_sha256` no longer matches the
  packet a new review would render. The existing push-time checks refuse the old
  approval on their own. What the candidate must add is that the record's review
  round is reset so the loop *asks* for the new review instead of parking.

Serialisation itself: `merge_sweep` already merges under one open window and is
all-or-nothing. What changes is that each merge inside the sweep moves the base
for every candidate that is not it, so the sweep must re-evaluate the obligation
between merges rather than once at the start.

**All of this is gated on `lanes > 1`.** At `lanes = 1` the window predicate
behaves exactly as it does today — the blanket block — so the existing tests
that pin it need no edit. This is also why the merge candidate can be built
early: it needs two execution *records*, which a test writes directly, not two
live agents.

### Decision 7 — observability: N lanes, truthfully

The brief's rule is the design: reporting the first lane's phase as the system's
phase would be worse than reporting nothing.

* **`phase` belongs to a lane.** Each lane's state file carries its own; the
  fleet record carries no `phase` field at all. At `lanes = 1` the only state
  file is `state.json` and every existing reader sees what it sees today.
* **`health.check` grows a per-lane pass.** It returns the fleet verdict plus
  one `Health` per lane, each using the existing `health.VERDICT_CODES`
  vocabulary. Any word the fleet needs that does not exist yet lives in
  `health.py` beside the others — the `projects_status` precedent, which added
  exactly one word (`unknown`) and put it there rather than in a second
  vocabulary.
* **The fleet verdict is the most severe lane verdict, never lane 0's by
  position.** The fleet needs attention if any lane does. At `lanes = 1` the
  fleet verdict *is* lane 0's verdict, unchanged, which is what keeps the
  existing exit codes and the existing cron wrappers working.
* **The dashboard grows a lanes panel** over the same aggregation, kept separate
  from its front door exactly as `dashboard.projects_status` is, so a later
  consumer renders the same rows rather than re-deriving them.
* **The `projects` view's `TASK` column** shows the fleet, not a lane: at
  `lanes > 1` it reads `3 lanes: brw-19 +2` — the oldest in-flight task and how
  many others — and never one lane's task as if it were the system's. `—`
  keeps meaning unreadable and must not be borrowed for "several".

### Decision 8 — a lane that dies mid-round

Most of the recovery already exists per task and is extended per lane rather
than rebuilt: `worker_repo_is_reusable` decides whether a worker can be resumed
as is, `WorkerRepoManager.quarantine` moves a failed attempt aside without
deleting evidence, execution records are per-task files that survive any
process, `merge_sweep.sweep_on_startup` reconciles the backlog on boot, and
`health.stranded_fault_rounds` already models a round that stopped mid-flight.

What the fleet adds:

* the supervisor notices a **dead lease** (by `LoopLock.is_live`'s rules) beside
  a state file that is mid-round, and recovers **that lane only** — its state
  file, its lease, its clone, its worker repo. No other lane names any of those,
  which is why the recovery cannot touch them.
* a dead lane **holding the merge token releases it**. The token is a lease with
  the same liveness rule, for the same reason: a merge slot held by a dead
  process is the one shared resource a lane death can strand.
* a lane whose observed clone is unclean or diverged is **not** silently rebuilt.
  `ObservedCheckout.synchronize` already refuses and says so; under the fleet
  that refusal is a lane-fatal park naming that lane's directory, and the other
  lanes keep running.

### An interaction the scheduler must answer: self-upgrade boundaries

`_self_upgrade_due` offers a replacement only at a `READY` phase with no packet
in flight. With N lanes running continuously that moment may never arrive, and a
merged upgrade would sit on disk forever — the silent-no-outcome failure the
self-upgrade section above exists to end, reintroduced by concurrency.

The answer belongs to the scheduler candidate and is named here so it is not
discovered late: when an upgrade is pending the supervisor enters a **drain** —
it admits no new tasks, lets the live lanes finish, and reaches the boundary with
the whole fleet idle. `os.execv` still replaces one process holding one lock, so
nothing about the handoff, the token, the one-shot marker or the decline set
changes.

### The split, in dependency order

Nine candidates. Each is independently reviewable and each leaves the loop
working; the concurrency setting stays at `1` until the last one.

| # | id | What it lands |
|---|---|---|
| 1 | conc-02 | the `[concurrency]` setting and the lane vocabulary; no behaviour change |
| 2 | conc-03 | rebase-aware, re-reviewed, serialised merge, behind `lanes > 1` |
| 3 | conc-04 | one observed checkout per lane — the isolation boundary |
| 4 | conc-05 | per-lane state files and lane leases |
| 5 | conc-06 | the fleet supervisor: scheduling, admission control, the cap, the drain |
| 6 | conc-07 | fault isolation: `lane_fatal` vs `fleet_fatal` |
| 7 | conc-08 | lane death and recovery |
| 8 | conc-09 | observability: `health` lanes, the dashboard panel, the `projects` column |
| 9 | conc-10 | turn it on: the N=2 end-to-end acceptance round and the operator docs |

**Why the merge candidate comes second and not last.** Both orderings were
considered. The brief lists the obstacles hardest-first — isolation, then the
state machine, then merging — and that is the right order of *difficulty*, not
of *work*. The merge obligation is the only piece of this that is provable
without any N>1 fixture at all: an operator merge moves the base identically to a
sibling lane's merge, so two execution records written by a test exercise the
whole protocol. It also de-risks the most common blocker code in the system
before a single lane exists. Isolation follows immediately, because it is the
one that breaks on the first concurrent round.

#### 1. conc-02 — the concurrency setting and the lane vocabulary

*Claim:* a `lanes` setting exists, is validated, and changes nothing at `1`.

*Scope:* `autoloop/config.py`, `autoloop/config.example.toml`,
`autoloop/tests/`.

*Tests:* absent `[concurrency]` resolves to 1; `0`, `-1`, `"two"`, a float and a
value over the ceiling each raise `ConfigError` naming the key; a lane id is
derived deterministically and is refused as a task id (`validate_task_id`'s
namespace must not collide with `workers_root`'s entries); the full existing
suite passes with no test edited.

#### 2. conc-03 — rebase-aware, re-reviewed, serialised merge

*Claim:* a candidate whose base moved is carried forward and re-reviewed, never
merged or pushed on its old approval; at `lanes = 1` the merge window is
byte-identical to today's.

*Scope:* `autoloop/cli.py` (`_merge_window_blockers`), `autoloop/auto_merge.py`,
`autoloop/merge_sweep.py`, `autoloop/orchestrator.py`, `autoloop/tests/`.

*Tests:* with `lanes = 1` and a candidate bound to the head, the window is shut
with today's reason and today's wording; with `lanes > 1` the window opens and
each bound candidate is recorded as owing a re-review; after a carry-forward the
candidate sha and tree sha have moved and `_dispatch_task_push` refuses the old
`PostcommitBinding`; a carry-forward that conflicts parks
`task_base_behind_head` and leaves the worker repo and the record intact; a
sweep of three branches re-evaluates the obligation between merges rather than
once; the all-or-nothing property of the sweep is unchanged.

#### 3. conc-04 — one observed checkout per lane

*Claim:* each lane's escape detector brackets a tree only that lane writes; a
genuine escape inside a lane is still detected; a write by one lane is not
attributed to another.

*Scope:* `autoloop/worker_env.py`, `autoloop/config.py`,
`autoloop/orchestrator.py`, `autoloop/escape_detector.py` (docstring only),
`autoloop/tests/`.

*Tests:* two lanes' clones resolve to different directories and
`validate_observed_checkout` refuses either nested in the other; lane B
synchronising mid-window produces **no** violation in lane A; a file written
into lane A's clone during lane A's window is still reported with its path; a
worker repo created for lane A records lane A's clone in `.git/FETCH_HEAD`; at
`lanes = 1` the clone path is exactly `[paths].observed_checkout` as resolved
today.

#### 4. conc-05 — per-lane state files and lane leases

*Claim:* N lanes hold N independent state machines; lane 0 at `lanes = 1`
writes literally `state.json` at today's path; two processes cannot enter one
lane.

*Scope:* `autoloop/state.py`, `autoloop/lock.py`, `autoloop/orchestrator.py`,
`autoloop/cli.py`, `autoloop/tests/`.

*Tests:* at `lanes = 1` no new file appears under the state dir and every
existing state test passes unedited; at `lanes = 2` each lane's phase advances
without touching the other's file; a live lease refuses a second entrant; a
lease predating boot is dead however its pid probes; `unlock` still refuses a
live fleet lock; a corrupt lease record refuses rather than reading as free.

#### 5. conc-06 — the fleet supervisor

*Claim:* the supervisor owns scheduling across N lanes, enforces the cap and the
admission rule, and reaches a self-upgrade boundary by draining.

*Scope:* `autoloop/orchestrator.py`, `autoloop/tasks.py`, `autoloop/cli.py`,
`autoloop/tests/`.

*Tests:* with more READY tasks than lanes, exactly `lanes` are dispatched and
the rest stay `pending` with no attempt charged; two tasks declaring the same
file entry are not co-scheduled while two sharing only `autoloop/tests/` and the
universal trackers are; a pending upgrade stops admission and the boundary is
reached once the last lane finishes; at `lanes = 1` the dispatch sequence is the
one the existing continuous-mode tests already pin.

#### 6. conc-07 — fault isolation

*Claim:* a lane-fatal condition stops one lane and no other; an unrecognised
kind still stops everything.

*Scope:* `autoloop/orchestrator.py`, `autoloop/blockers.py`, `autoloop/cli.py`,
`autoloop/tests/`.

*Tests:* every existing `loop_fatal` call site is classified explicitly by the
diff (a table in the candidate's own report); a `lane_fatal` park leaves the
other lanes advancing; a blocker record names the lane; a park with no kind, or
an unknown kind, stops the fleet; `checkout_escape_detected` stops the fleet.

#### 7. conc-08 — lane death and recovery

*Claim:* a lane that dies mid-round is recovered without touching the others.

*Scope:* `autoloop/orchestrator.py`, `autoloop/worker_env.py`,
`autoloop/merge_sweep.py`, `autoloop/health.py`, `autoloop/tests/`.

*Tests:* a lane killed mid-executing is resumed or quarantined on the next tick
while the other lanes' state files, clones and worker repos are byte-identical
before and after; a dead lane holding the merge token releases it and a live
lane then merges; an unclean clone parks that lane and no other; a dead lane
whose worker repo fails `worker_repo_is_reusable` is quarantined rather than
reused.

#### 8. conc-09 — observability

*Claim:* `health` and the dashboard report every lane truthfully, and no single
string is presented as the fleet's phase.

*Scope:* `autoloop/health.py`, `autoloop/dashboard.py`, `autoloop/tests/`.

*Tests:* `health --json` carries one object per lane, each with a code from
`VERDICT_CODES`; the fleet code is the most severe lane code and is not lane 0's
by position; at `lanes = 1` the JSON, the text and the exit code are unchanged
against a snapshot of today's output; a fleet at its cap is distinguishable from
an idle fleet; the dashboard renders N lanes and takes no lock.

#### 9. conc-10 — turn it on

*Claim:* N tasks are implemented concurrently, each producing an independently
reviewable candidate, and the candidates reach the base one at a time with none
stranded.

*Scope:* `autoloop/config.example.toml`, `docs/AUTOLOOP.md`,
`autoloop/tests/`.

*Tests:* an end-to-end round at `lanes = 2` with a stub executor: two candidates
produced, two independent reviews, two merges one at a time, the second rebased
and re-reviewed against the first; the shipped default stays `1`.

### Where each of the brief's required tests is proved

| conc-01 asked for | proved by |
|---|---|
| N tasks run concurrently, each an independently reviewable candidate | conc-10, on the mechanisms of conc-04/05/06 |
| a write by one lane is not attributed to another | conc-04 |
| a genuine escape inside one lane is still detected | conc-04 |
| candidates merge one at a time | conc-03 |
| a base that moved is rebased and re-reviewed, never merged on the old approval | conc-03 |
| a lane that dies mid-round is recovered without touching the others | conc-08 |
| N=1 behaves exactly as today, existing tests unchanged | every candidate; see below |
| health and the dashboard report every lane | conc-09 |

### The acceptance criterion every candidate carries

**At `lanes = 1` the existing suite passes with zero test edits.** Not "passes
after the obvious updates" — zero. Lane 0 keeps writing literally `state.json`,
the fleet lock keeps the path and the semantics `LoopLock` has today, the merge
window keeps today's predicate and today's wording, and `health`'s output is
unchanged byte for byte. If a candidate finds itself editing an existing test,
that is the signal that the N=1 path moved, and the right response is to fix the
candidate rather than the test.

### What this plan does not decide

* **Which existing `loop_fatal` codes become `lane_fatal`.** That is an
  enumeration against real call sites and belongs to conc-07's diff, not to a
  guess made here.
* **The hard ceiling on `lanes`.** It should be measured against the executor's
  actual concurrency behaviour and the machine, not asserted; conc-02 picks a
  conservative number and says how it was chosen.
* **Containers.** The brief offers "a git worktree or a container per task". A
  container would close the write-then-revert and `.git/`-internals residuals a
  clone cannot, and it is a different, larger project (docs/SECURITY.md S24). A
  linked git worktree is explicitly *not* the answer — `worker_env.py`'s module
  docstring records why: a worktree shares its `.git`, and therefore every
  remote, hook and credential-relevant config key, with the checkout it came
  from.
* **A provider with a shared conversation.** Every codex turn is its own
  subprocess, so nothing here designs around one.
