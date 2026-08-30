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
separate branch, separate browser profile — works: `LoopLock` is scoped to a
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
| `self_upgrade_unapplicable` | the merge moved a different checkout | settled |
| `self_upgrade_preflight_failed` | the merged tree does not import | settled, `detail` carries the error |
| `self_upgrade_exec_failed` | marker unwritable, lock unarmable, or `execv` refused | settled |
| `self_upgrade_deferred` | this process may not hand off | **stays pending** |
| `self_upgrade_none` | nothing pending was left to act on | no record |

**No outcome but `exec` ends the process.** A settled outcome means the loop
carries on with the code it has, which was working a second ago; exiting is the
one response that guarantees no further work happens. A failed preflight in
particular is *reported and refused*, never fatal — replacing a working loop
with a tree that cannot start is the failure that check exists to prevent.

**`deferred` is the one outcome that leaves the record pending**, and it is the
one an operator acts on. It means the boundary was reached by a **single-round**
`run` — `run` with no `--continuous`, and therefore also `--retry`, `--answer`,
`--resubmit` and `resume`, all of which funnel into the same path. Such a
process cannot hand off, because its own command line manages ONE round:
`--kickoff` refuses a session that now exists and `--answer` refuses a phase
that is no longer `needs_user`, so the successor would die on a `StateError`
instead of continuing the loop. Nothing about the merged tree has been judged,
so nothing is settled: the loop finishes the session it is in, and the upgrade
waits. Perform it with

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
timer: an `execed` record is never offered again, so a successor that dies
before completing one iteration is not retried (`_confirm_self_upgrade` retires
the marker after one full pass); every settled outcome has left `pending`, so it
cannot come back round; and a `deferred` boundary is declined for that
`base_sha` in the declining process, so the round it carries on into is not
offered the same upgrade again. A later merge is a different `base_sha` and does
get its own boundary.

**The successor's command line is `python -m autoloop run --continuous`**, plus
`--config` and `--null-executor` when this process had them — rebuilt, not the
verb you typed. `os.execv` preserves the pid and the lock is never released
(that is the point), so a successor re-running `start` would read a live lock
naming itself, print "already running — nothing to do" and exit: a successful
handoff that still ends the loop. It is recorded in the `argv` field of
`self_upgrade_exec`.
