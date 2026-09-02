"""Single-instance locking for a state directory.

Exactly one autoloop process may operate on a state dir at a time. The lock is
a JSON file created with O_CREAT|O_EXCL (atomic on POSIX); it records pid,
hostname, start time, run id and the state dir, so a crashed run stays
diagnosable.

Semantics — deliberately fail-closed:

* An existing lock whose owner is a LIVE process (same host, pid alive) →
  LockHeldError. Waiting or stopping that process are the only options.
* An existing lock from a DIFFERENT host, or whose pid we cannot probe →
  treated as live (we cannot prove it dead), still LockHeldError.
* An existing lock written BEFORE the machine's current boot → provably
  stale whatever its pid says. Pids are reassigned across a reboot, so a
  lock left behind by a power-off can otherwise name a pid that some
  unrelated process now holds, and `os.kill(pid, 0)` reports it live. That
  turns the documented recovery into a dead end pointing at an innocent
  process. The boot check runs BEFORE the pid probe for exactly that
  reason; when boot time cannot be read the probe still decides, so this
  can only ever declare MORE locks recoverable, never fewer.
* An existing lock whose owner is verifiably dead → StaleLockError, which
  names the explicit recovery command. Locks are NEVER stolen silently —
  recovery is `python -m autoloop unlock`, which itself refuses live locks.
* Clean exit releases the lock (context-manager), and only if we still own it
  (run id match), so `unlock` + a new run can never be un-done by a zombie.

## The one exception: a lock this very process handed to itself

`cli._self_upgrade_at_boundary` replaces the loop's interpreter with
`os.execv` so a merge that changed `autoloop/` actually runs. `execv` REPLACES
the process image — the pid survives, no `finally` runs, so the lock file is
never released and never has to be: from outside, one live pid holds one lock
file across the whole replacement, with no instant at which another process
could take it. That is the point of `execv` rather than spawn-and-exit.

But the new image re-runs `_cmd_run`, which acquires the lock again with a
fresh run id — and would find a lock naming a LIVE pid (its own) and fail
closed. So `acquire` adopts, on evidence and only on evidence:

* the lock must carry an `exec_handoff` marker, written by
  `mark_exec_handoff` immediately before `execv`,
* naming this hostname and THIS pid, twice — as the lock's owner and inside
  the marker,
* naming the run the lock itself records, so a marker cannot describe a
  handoff of some other run's lock,
* carrying a TOKEN that matches the one this process inherited in its
  environment (`AUTOLOOP_EXEC_HANDOFF_TOKEN`),
* and the adoption CLEARS the marker and CONSUMES the environment token, so
  it can be used exactly once.

The token is what makes the marker's other three facts unforgeable. Everything
else in it is guessable or reproducible from outside: pids are small integers
that get reused within a boot, the hostname is public, and the lock file's own
run id is readable by anything that can read the lock. So a marker left on disk
by a run that died — or written by any process that can write the state dir —
plus an unlucky pid reassignment would otherwise be enough to walk past a live
lock. The token is 32 random bytes minted immediately before the `execv`, never
written anywhere but the lock file, and reaches the successor ONLY because
`os.execv` inherits this process's environment. A process that did not receive
it cannot produce it, whatever the lock file says.

A live lock without that marker is still `LockHeldError`, and so is one whose
marker names a different pid, a different host, a different run, or a token
this process did not inherit. `started_at` is preserved across the adoption
because the lock really has been held continuously since then — which also
keeps the predates-boot check honest.

## Lane leases (conc-05)

`LoopLock` above is the FLEET lock and is unchanged: one holder per state dir,
`state_dir/LOCK`, the same adoption rule, the same refusals, and `unlock`
refusing a live one exactly as it always did. Below it sits `LaneLease` — one
per lane, a small record beside that lane's own state file, answering the
narrower question "is a process already IN this lane?".

Its liveness predicate is BORROWED, not written a second time: `LaneLease.
is_live` builds a `LockInfo` and asks `LoopLock.is_live`, so a foreign host
reads as live, a lease predating boot is dead however its pid probes, and a pid
probe decides the rest — with no possibility of the two drifting apart. That is
the plan's own argument for reuse (docs/AUTOLOOP.md, "Decision 2"): two
implementations of "is it alive" drift, and the one that drifts is the one that
lets two processes into one lane.

`cli._LaneEntry` is what takes one, around the lane's work and only at
`lanes > 1`; the two locks are never held by the same object, and the one
place they interact is the self-upgrade handoff, where the fleet lock is armed
to SURVIVE `os.execv` and the lease is released so that it does not. See
`LaneLease`'s own docstring for both halves.

## The other fleet-scoped record: `fleet_throttle.json` (conc-11)

`state.FleetThrottleStore` keeps ONE rate-limit episode — a shared
`retry_not_before` and a shared consecutive-back-off counter — in a file BESIDE
`state_dir/LOCK`, and it is there for the reason the lock is: one state
directory is one account's fleet, so "this account is throttled" is a fact about
the directory rather than about any lane. N lanes draw on ONE ChatGPT allowance,
and conc-05's per-lane state files would otherwise have turned one limit into N
independent counters.

**It is NOT a lock and takes none of the semantics above.** It is never
acquired, never owned, never adopted across `os.execv`, and holding it is not a
thing a process does — every lane reads it and any lane may write it. What it
borrows is the location and one discipline: a record it cannot READ is refused
rather than read as "no throttle" (see that class), the same direction
`LaneLease.read` fails in. `unlock` does not touch it, and neither does
`break_stale`; nothing here does.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .errors import LockHeldError, StaleLockError, StateCorruptError
from .state import lane_paths, utcnow_iso

LOCK_FILENAME = "LOCK"

#: Where the exec-handoff token travels. `os.execv` inherits the environment of
#: the process it replaces, and nothing else does — the successor image reads
#: this variable, matches it against the token in the lock file, and adopts the
#: lock only if the two agree. Deliberately NOT a second file: a file is
#: readable by whatever can read the lock, which is exactly the property the
#: token exists to deny.
EXEC_HANDOFF_TOKEN_ENV = "AUTOLOOP_EXEC_HANDOFF_TOKEN"

#: Bytes of randomness per handoff token (`secrets.token_hex` doubles this in
#: characters). One token authorizes one adoption of one live lock, so it is
#: sized to be unguessable rather than to be typed.
EXEC_HANDOFF_TOKEN_BYTES = 32

#: How far a lock's `started_at` must predate boot before we call it stale.
#: `started_at` has one-second resolution and wall clocks get adjusted (NTP
#: steps, a timezone-confused RTC), so a lock written seconds either side of
#: boot is not evidence of anything. Slack buys safety in one direction only:
#: it can leave a genuinely dead lock needing the pid probe, never declare a
#: live one recoverable.
BOOT_CLOCK_SLACK_SECONDS = 120.0


@dataclass(frozen=True)
class LockInfo:
    pid: int
    hostname: str
    started_at: str
    run_id: str
    state_dir: str
    #: Set ONLY between `mark_exec_handoff` and the `os.execv` that follows it
    #: — `{"pid", "run_id", "at", "reason", "token"}`. Its presence is what
    #: lets the replacement image adopt this lock instead of failing closed on
    #: its own live pid, but only together with the matching `token` inherited
    #: through `EXEC_HANDOFF_TOKEN_ENV`; adopting clears both. Absent (the
    #: normal state) for every lock this package has ever written, which is why
    #: `read` defaults it rather than requiring it.
    exec_handoff: dict | None = None

    def describe(self) -> str:
        return (
            f"pid={self.pid} host={self.hostname} started={self.started_at} "
            f"run_id={self.run_id}"
        )


def boot_time_epoch() -> float | None:
    """Unix time of this machine's last boot, or None if it can't be read.

    Deliberately NOT derived from a monotonic clock. On macOS
    `CLOCK_MONOTONIC` stops during sleep, so uptime-minus-now on a laptop
    that has slept reports a boot far more recent than the real one — which
    would make a LIVE lock look like it predates boot. `kern.boottime` and
    `/proc/stat`'s `btime` are the authoritative values on their platforms;
    anywhere else we return None and let the pid probe decide.
    """
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            # `{ sec = 1754126400, usec = 837291 } Sat Aug  2 ...`
            marker = "sec = "
            start = out.index(marker) + len(marker)
            end = start
            while end < len(out) and out[end].isdigit():
                end += 1
            return float(out[start:end])
        if sys.platform.startswith("linux"):
            for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _predates_boot(started_at: str) -> bool:
    """True only when `started_at` is provably older than the current boot."""
    boot = boot_time_epoch()
    if boot is None:
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if started.tzinfo is None:
        # A naive stamp cannot be compared to an absolute epoch without
        # guessing a timezone, and guessing wrong here would break a live
        # lock. `utcnow_iso` is always tz-aware; anything else is foreign.
        return False
    return started.timestamp() < boot - BOOT_CLOCK_SLACK_SECONDS


def _token_matches(recorded: object, inherited: object) -> bool:
    """Constant-time equality for two handoff tokens — and False, never a
    raise, for anything that is not a token at all.

    Both sides come from outside this function's control: one off the lock
    file, one out of the environment. `secrets.compare_digest` raises
    `TypeError` on a non-`str` and on a `str` with any non-ASCII character, and
    a raise here would surface inside `acquire` — the successor's FIRST act
    after `execv`, with no `finally` behind it. That is a crash, not a
    refusal, and this module's whole discipline is that anything it cannot
    verify is refused.
    """
    if not isinstance(recorded, str) or not isinstance(inherited, str):
        return False
    if not recorded or not inherited:
        return False
    if not recorded.isascii() or not inherited.isascii():
        return False
    return secrets.compare_digest(recorded, inherited)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


class LoopLock:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / LOCK_FILENAME
        self.run_id = uuid.uuid4().hex
        self._owned = False
        self._prev_handlers: list[tuple[int, object]] = []
        #: The run id this lock was ADOPTED from, when `acquire` took over a
        #: lock this same pid handed to itself across `os.execv`. Empty for an
        #: ordinary acquisition, so a caller can report the continuity.
        self.adopted_run_id = ""

    # ---- inspection ---------------------------------------------------------

    def read(self) -> LockInfo | None:
        """Current lock info, or None if no lock file exists. A corrupt lock
        file returns a LockInfo with pid=-1 (never provably dead → held)."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            handoff = data.get("exec_handoff")
            return LockInfo(
                pid=int(data["pid"]),
                hostname=str(data["hostname"]),
                started_at=str(data["started_at"]),
                run_id=str(data["run_id"]),
                state_dir=str(data["state_dir"]),
                exec_handoff=handoff if isinstance(handoff, dict) else None,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return LockInfo(
                pid=-1,
                hostname="(corrupt lock file)",
                started_at="?",
                run_id="?",
                state_dir=str(self.state_dir),
            )

    @staticmethod
    def is_live(info: LockInfo) -> bool:
        if info.pid == -1:
            return False  # corrupt file: nothing live can renew it — stale
        if info.hostname != socket.gethostname():
            return True  # cannot verify a foreign host's pid — fail closed
        if _predates_boot(info.started_at):
            # Written before this boot, so its owner did not survive to
            # here. Checked ahead of the probe because the pid it names may
            # since have been handed to an unrelated process.
            return False
        return _pid_alive(info.pid)

    # ---- lifecycle ----------------------------------------------------------

    # ---- termination handling ----------------------------------------------
    #
    # Ctrl-C already unwinds cleanly: KeyboardInterrupt runs `__exit__`.
    # Python's default action for SIGTERM and SIGHUP is to die WITHOUT running
    # `finally`, so the orderly-looking ways to stop (a shutdown, a logout,
    # plain `kill`) were the ones that left a lock behind. This lives on the
    # lock rather than at one call site because every holder needs it — `run`
    # is the long one, but `smoke-browser` drives a real browser and
    # `review-changeset` waits on a reviewer, and a per-command wrapper would
    # protect whichever ones somebody remembered.
    #
    # The release happens INSIDE the handler, before unwinding, and that is
    # the point rather than a detail: a SIGTERM mid-fan-out unwinds into
    # `ThreadPoolExecutor.shutdown(wait=True)`, which waits on agents that run
    # for minutes, while a shutdown's grace period is seconds. Nothing else is
    # written here — state is consistent at every instant (atomic saves), and
    # touching the pause flag would silently gag the NEXT run.

    def _install_termination_handlers(self) -> None:
        def _terminate(signum, _frame):
            self.release()
            raise SystemExit(128 + signum)

        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                self._prev_handlers.append((signum, signal.signal(signum, _terminate)))
            except ValueError:
                # Not the main thread (embedded or test use). The caller's own
                # cleanup still applies; we simply cannot add to it here.
                pass

    def _restore_termination_handlers(self) -> None:
        while self._prev_handlers:
            signum, previous = self._prev_handlers.pop()
            try:
                signal.signal(signum, previous)
            except ValueError:
                pass

    def acquire(self) -> "LoopLock":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        info = LockInfo(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=utcnow_iso(),
            run_id=self.run_id,
            state_dir=str(self.state_dir),
        )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = self.read()
            if existing is None:  # raced with a release; one retry
                return self.acquire()
            if self._is_handoff_to_this_process(existing):
                return self._adopt(existing)
            if self.is_live(existing):
                raise LockHeldError(
                    f"another autoloop process holds {self.path} "
                    f"({existing.describe()}). Wait for it or stop it — "
                    "locks are never stolen."
                ) from None
            raise StaleLockError(
                f"stale lock at {self.path} ({existing.describe()}) — its owner "
                "is no longer running. Inspect it, then recover with: "
                "python -m autoloop unlock"
            ) from None
        try:
            os.write(fd, json.dumps(asdict(info), indent=2).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._owned = True
        self._install_termination_handlers()
        return self

    # ---- self-upgrade handoff (`os.execv`, same pid) ------------------------

    def _write(self, info: LockInfo) -> None:
        """Replace the lock file's contents in one step — temp file in the same
        directory, fsync, `os.replace`.

        Never unlink-then-recreate. An unlink would open a window in which the
        lock does not exist, which is exactly the window "the lock stays valid
        across the replacement" denies; `os.replace` is atomic on POSIX, so a
        concurrent `read()` sees the old bytes or the new ones and never
        nothing.
        """
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(fd, json.dumps(asdict(info), indent=2).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    def _is_handoff_to_this_process(self, info: LockInfo) -> bool:
        """Is this lock one THIS process handed to itself across `os.execv`?

        Five independent facts, all required: the marker exists; the lock is
        ours by host; it names THIS pid, both as the lock's owner and inside
        the marker; the marker names the run the lock itself records; and its
        token matches the one this process INHERITED in its environment.

        Asked this narrowly because the weaker rule — "the lock's pid is my
        pid, so it is mine" — is a lock-stealing hole: pids are reused within a
        boot, so a dead run's lock plus an unlucky pid assignment would let an
        unrelated process walk past a refusal the rest of this module exists to
        make. The token is what closes the remaining gap in the marker itself:
        pid, host and run id are all readable or reproducible from outside, so
        a stale-but-valid-looking marker on disk is not evidence of a handoff.
        The token was minted for one `execv` and reached here only by being
        inherited across it.
        """
        handoff = info.exec_handoff
        if not isinstance(handoff, dict):
            return False
        if info.hostname != socket.gethostname():
            return False
        me = os.getpid()
        if info.pid != me or handoff.get("pid") != me:
            return False
        if handoff.get("run_id") != info.run_id:
            return False
        return _token_matches(handoff.get("token"), os.environ.get(EXEC_HANDOFF_TOKEN_ENV))

    def _adopt(self, existing: LockInfo) -> "LoopLock":
        """Take over the lock this process left for itself, under a new run id.

        `started_at` is carried over: the lock genuinely has been held since
        then (same pid, no gap), and rewriting it to now would misreport how
        long this state dir has been locked and weaken the predates-boot check.
        The marker is dropped AND the inherited token is consumed out of the
        environment, so the adoption cannot happen twice — and so nothing this
        process later spawns inherits an authorization it has already spent.
        """
        self._write(
            LockInfo(
                pid=os.getpid(),
                hostname=existing.hostname,
                started_at=existing.started_at,
                run_id=self.run_id,
                state_dir=str(self.state_dir),
            )
        )
        os.environ.pop(EXEC_HANDOFF_TOKEN_ENV, None)
        self.adopted_run_id = existing.run_id
        self._owned = True
        self._install_termination_handlers()
        return self

    def mark_exec_handoff(self, reason: str = "") -> bool:
        """Arm the lock for an `os.execv` in THIS process. Returns whether it
        was armed.

        Called immediately before the exec and nowhere else. Refuses unless we
        own the lock and the file still names our run — the caller treats a
        `False` here as "do not replace the process", because a replacement
        whose successor cannot acquire the lock would end the run.

        Arming is two writes that must not come apart: a fresh unguessable
        token into the environment (which `os.execv` inherits and nothing else
        does) and the same token into the lock file. The environment goes
        FIRST, so there is no instant at which the marker is on disk without
        its token being inheritable — that marker could never be adopted, and
        the caller has by then already spent the upgrade's one shot. The
        reverse leftover is inert: an environment token with no marker on disk
        authorizes nothing, since adoption requires both.
        """
        if not self._owned:
            return False
        current = self.read()
        if current is None or current.run_id != self.run_id:
            return False
        token = secrets.token_hex(EXEC_HANDOFF_TOKEN_BYTES)
        os.environ[EXEC_HANDOFF_TOKEN_ENV] = token
        try:
            self._write(
                LockInfo(
                    pid=current.pid,
                    hostname=current.hostname,
                    started_at=current.started_at,
                    run_id=current.run_id,
                    state_dir=current.state_dir,
                    exec_handoff={
                        "pid": os.getpid(),
                        "run_id": self.run_id,
                        "at": utcnow_iso(),
                        "reason": reason,
                        "token": token,
                    },
                )
            )
        except OSError:
            os.environ.pop(EXEC_HANDOFF_TOKEN_ENV, None)
            return False
        return True

    def clear_exec_handoff(self) -> None:
        """Disarm it again — the exec did not happen (`os.execv` itself
        refused). Best effort on the FILE: a marker left behind is inert once
        the token is gone, so a failure here must not propagate into a run that
        is otherwise fine.

        The environment token is dropped first and unconditionally. It is the
        half that would otherwise be inherited by every subprocess this run
        goes on to spawn, and it is the half whose removal alone already makes
        the marker unusable.
        """
        os.environ.pop(EXEC_HANDOFF_TOKEN_ENV, None)
        if not self._owned:
            return
        current = self.read()
        if current is None or current.run_id != self.run_id:
            return
        try:
            self._write(
                LockInfo(
                    pid=current.pid,
                    hostname=current.hostname,
                    started_at=current.started_at,
                    run_id=current.run_id,
                    state_dir=current.state_dir,
                )
            )
        except OSError:
            pass

    def release(self) -> None:
        if not self._owned:
            return
        current = self.read()
        if current is not None and current.run_id == self.run_id:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._owned = False
        self._restore_termination_handlers()

    def __enter__(self) -> "LoopLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    # ---- explicit recovery (the `unlock` command) ---------------------------

    def break_stale(self) -> LockInfo:
        """Remove a verifiably-stale lock. Refuses live locks — there is no
        force path past a running process."""
        info = self.read()
        if info is None:
            raise StaleLockError(f"no lock file at {self.path} — nothing to recover")
        if self.is_live(info):
            raise LockHeldError(
                f"refusing to remove a LIVE lock ({info.describe()}). "
                "Stop that process instead."
            )
        self.path.unlink()
        return info


# ---- lane leases (conc-05) --------------------------------------------------


@dataclass(frozen=True)
class LaneLeaseInfo:
    """Who is in one lane, as it is written to disk.

    The same four facts a `LockInfo` carries — pid, hostname, `started_at`, run
    id — plus WHICH LANE, and nothing else. Deliberately not a subclass of
    `LockInfo`: that would inherit `exec_handoff`, serialise it into every
    lease file as `null`, and invite a reader into believing a lease can be
    adopted across `os.execv`. It cannot; only the fleet lock can, and only the
    fleet lock has the token machinery that makes that safe.

    `lane_id` is carried in the RECORD as well as in the file name so a lease
    that has been copied or moved between lanes is refused rather than honoured
    — see `LaneLease.read`, which checks it against the lane doing the reading.
    """

    pid: int
    hostname: str
    started_at: str
    run_id: str
    lane_id: str
    #: The lane's own directory (`state.lane_paths(...).state_dir`), recorded
    #: for the same diagnostic reason `LockInfo.state_dir` is: a lease found in
    #: a backup says which tree it belonged to.
    state_dir: str

    def describe(self) -> str:
        return (
            f"lane={self.lane_id} pid={self.pid} host={self.hostname} "
            f"started={self.started_at} run_id={self.run_id}"
        )


class LaneLease:
    """Exclusive entry to ONE lane — `<lane_id>.lease.json` beside that lane's
    state file (docs/AUTOLOOP.md, "Decision 2 — one fleet lock, N lane leases,
    N state files").

    Three properties, each of which is a way this could have failed open:

    * **Acquisition is `O_CREAT|O_EXCL` and nothing else.** That single call is
      the whole of the mutual exclusion, and it is atomic on POSIX. No path in
      this class unlinks or overwrites a lease it did not create, so there is no
      window in which two processes can both conclude the lane is theirs.
    * **A DEAD lease is refused, not taken over.** This is the deliberate
      choice, and the reason is a race rather than a preference: "read the
      record, decide it is dead, overwrite it" is a check-then-act on a shared
      file, and two processes that both read the same dead lease both overwrite
      and both enter. Refusing keeps the only acquisition path the atomic one.
      It is also this module's existing discipline — locks are never stolen
      silently — and recovering a dead lane is candidate 7 of the split plan
      ("Decision 8 — a lane that dies mid-round"), which does it from the fleet
      supervisor, holding the fleet lock, so that no second entrant can exist
      while it runs. `break_stale` below is the primitive it will call.
    * **An unreadable lease is refused rather than read as free.** See `read`.

    WHO TAKES ONE: `cli._LaneEntry`, wrapped around the lane's own work inside
    `cli._cmd_run` — after the fleet lock is held, and inside the `try` whose
    `finally` publishes the heartbeat, so every exit that unwinds releases it.
    `run` and `start` (which ends in `_cmd_run`) are the only entry points that
    run a lane today; the fleet supervisor of candidate 5 will hold one of the
    same objects per lane instead of one per process.

    ONLY AT `lanes > 1`. That gate is the acceptance criterion made structural
    rather than a flag that could be wrong: at `lanes = 1` nobody asks for a
    lease, so no lease file is created at all and "no new file appears under the
    state dir" stays true by construction. Lane 0's exclusion at one lane is the
    fleet lock's, and always has been.

    THE ONE EXIT THAT DOES NOT UNWIND is `os.execv`, and it is why this class
    has no handoff of its own and needs none. `cli._self_upgrade_at_boundary`
    replaces the process to pick up a merged upgrade; the fleet lock survives
    that by a token-authenticated adoption and a lease has nothing of the sort,
    so held across the replacement it would be a LIVE lease naming the
    successor's own pid and the successor would fail closed — a self-upgrade
    would end the run. The lease is therefore RELEASED in the instant before the
    exec, immediately after the lock is armed, and the handoff is REFUSED
    (`exec_failed`, with the lock disarmed again) when it cannot be — the same
    refusal, for the same reason, as a lock that could not be armed. An
    `os.execv` that raises re-enters the lane before the loop carries on, and
    fails closed if the lane is no longer enterable.

    NO SIGNAL HANDLERS, unlike `LoopLock`, and that is REQUIRED rather than
    merely economical. `signal.signal` replaces the previous handler: a lease
    installing its own would displace the lock's `_terminate`, and the fleet
    lock — the more important of the two — would stop being released on
    SIGTERM. It needs none of its own, because the lock's handler raises
    `SystemExit`, which unwinds through this class's `__exit__`.
    """

    def __init__(self, state_dir: Path, lane_index: int):
        #: Refuses a bool, a float and a negative before anything touches the
        #: filesystem — `state.lane_paths` explains why that ordering matters.
        paths = lane_paths(state_dir, lane_index)
        self.lane_index = lane_index
        self.lane_id = paths.lane_id
        self.lane_dir = paths.state_dir
        self.path = paths.lease_file
        self.run_id = uuid.uuid4().hex
        self._owned = False

    # ---- inspection ---------------------------------------------------------

    def read(self) -> LaneLeaseInfo | None:
        """The lease record, `None` when there is no lease, and
        `StateCorruptError` when there is one this cannot be trusted to read.

        REFUSING IS THE POINT, and the reason is worth stating exactly, because
        `acquire` alone would refuse either way. `LoopLock.read` answers a
        corrupt lock file with a `pid=-1` sentinel that reads as not-live; the
        same sentinel here would make an unreadable lease INDISTINGUISHABLE
        from a well-formed dead one — and "dead" is the verdict `break_stale`
        acts on, so it would unlink a record it could not read, which is how a
        lane with a live process still in it gets opened. Any caller asking
        `is_live(read())` directly would be told the same: the lane is free, on
        the strength of bytes nobody could parse. A lease is the only thing
        standing between two processes and one lane; if it cannot be read,
        nothing is known, and nothing known is not permission.

        The record is built with `LaneLeaseInfo(**data)` rather than by picking
        fields out one at a time, so an unknown key is a `TypeError` and a
        refusal. Field-picking would silently ignore anything it did not
        recognise — including a planted `exec_handoff`, which leases do not have
        and must not appear to.

        TYPES ARE CHECKED, not merely unpacked, for the reason
        `state.StopRepetitionStore.load` gives about its own `count`: `bool` is
        an `int` in Python, so a hand-written `"pid": true` would otherwise
        become pid 1 — a plausible-looking, almost certainly live pid — and a
        `"pid": "123"` would reach `os.kill` and raise from inside a liveness
        probe. A pid below 1 is refused for the same reason a count below 1 is:
        no writer here produces one, and `os.kill(0, 0)` addresses the caller's
        whole process group rather than the lease's owner.

        THE EMPTY FILE IS NOT HYPOTHETICAL: `acquire` creates the lease with
        `O_CREAT|O_EXCL` and writes it a moment later, so a process KILLED in
        that window leaves a zero-byte lease behind — or, killed between two
        partial writes of the record, a truncated one. `json.loads` raises on
        both, which is exactly right — a lease whose owner died before it could
        say who it was is not a lane anyone may enter on the strength of the
        file being there. An ordinary I/O failure in that same window no longer
        reaches here at all, and neither does a SHORT write: `acquire` writes
        the record in full or removes the file it has just proved it created,
        so only a killed process leaves one.

        A file that vanishes between the `exists()` and the read is `None`, not
        corrupt: that is a release racing this read, and the lane really is
        free. Anything else unreadable (permissions, an I/O error) refuses.
        """
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None  # released between the check and the read
        except OSError as exc:
            raise StateCorruptError(
                f"lane lease {self.path} cannot be read: {exc}. Until it can be, "
                f"nothing may enter lane {self.lane_id}."
            ) from exc
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError(f"expected a JSON object, got {type(data).__name__}")
            info = LaneLeaseInfo(**data)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise StateCorruptError(
                f"lane lease {self.path} is unreadable: {exc}. A lease that cannot "
                f"be read is NOT a free lane — refusing to enter {self.lane_id}."
            ) from exc
        if isinstance(info.pid, bool) or not isinstance(info.pid, int):
            raise StateCorruptError(
                f"lane lease {self.path} has a non-integer pid {info.pid!r}"
            )
        if info.pid < 1:
            raise StateCorruptError(
                f"lane lease {self.path} has pid {info.pid}, which no writer "
                "produces"
            )
        for name in ("hostname", "started_at", "run_id", "lane_id", "state_dir"):
            if not isinstance(getattr(info, name), str):
                raise StateCorruptError(
                    f"lane lease {self.path} has a non-string {name}"
                )
        if info.lane_id != self.lane_id:
            raise StateCorruptError(
                f"lane lease {self.path} names lane {info.lane_id!r}, not "
                f"{self.lane_id!r} — a lease that has been moved between lanes "
                "describes neither of them."
            )
        return info

    @staticmethod
    def is_live(info: LaneLeaseInfo) -> bool:
        """Exactly `LoopLock.is_live`, asked about a lease.

        Borrowed rather than reimplemented — the whole of the boot-before-probe
        ordering, the foreign-host fail-closed rule and the clock slack come
        with it, and cannot drift from the fleet lock's copy because there is no
        second copy. The `pid == -1` branch inside it is unreachable from here:
        `read` refuses any pid below 1 long before this is called, so the only
        records that reach the predicate are well-formed ones.
        """
        return LoopLock.is_live(
            LockInfo(
                pid=info.pid,
                hostname=info.hostname,
                started_at=info.started_at,
                run_id=info.run_id,
                state_dir=info.state_dir,
            )
        )

    # ---- lifecycle ----------------------------------------------------------

    def acquire(self) -> "LaneLease":
        """Enter the lane, or raise.

        `LockHeldError` when a live process is already in it, `StaleLockError`
        when the lease's owner is provably dead (see the class docstring for why
        that is a refusal rather than a takeover), and `StateCorruptError`
        when the lease cannot be read at all.

        The stale message deliberately does NOT name `python -m autoloop
        unlock`, though the exception type's own docstring does: that command
        breaks the FLEET lock and would not touch this file, so pointing an
        operator at it would be a remedy that silently does nothing.

        THE RECORD IS WRITTEN IN FULL OR NOT AT ALL, and "in full" is a loop
        rather than one call. `os.write` is a single `write(2)`: it may write
        FEWER bytes than it is given and return that count, raising nothing.
        That is not an error path — it is the quiet one, and it would leave a
        truncated lease that `read` must refuse forever, closing a lane with no
        process in it while its owner believed it had entered. A call that
        returns zero has moved nothing at all; it is raised as `OSError`
        (deliberately that type, so the cleanup below catches it) rather than
        retried, because retrying a write that cannot progress hangs the lane
        instead of refusing it. Ownership is claimed only after the last byte
        and the `fsync`.

        A WRITE THAT FAILS TAKES ITS OWN LEASE BACK OFF DISK, and that is the
        one removal in this class that does not read the record first — for the
        reason the class docstring gives about why every other one does.
        `O_CREAT|O_EXCL` has just proved this process CREATED the file, so there
        is no incumbent to steal from and no check-then-act to lose: what is
        being removed is this call's own failed acquisition, never somebody
        else's lease. Leaving it would be strictly worse than either outcome the
        refusals are about — an empty or half-written record that `read` must
        refuse forever, stranding a lane on an ENOSPC nobody would think to look
        for. The raise still propagates: a caller that could not take the lane
        is not in it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        info = LaneLeaseInfo(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=utcnow_iso(),
            run_id=self.run_id,
            lane_id=self.lane_id,
            state_dir=str(self.lane_dir),
        )
        # Rendered BEFORE the file exists, so a record that cannot be encoded
        # strands nothing: at this point there is no lease on disk to clean up.
        payload = json.dumps(asdict(info), indent=2).encode("utf-8")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = self.read()
            if existing is None:  # raced with a release; one retry
                return self.acquire()
            if self.is_live(existing):
                raise LockHeldError(
                    f"another process is in lane {self.lane_id} "
                    f"({existing.describe()}) — {self.path}. Wait for it or stop "
                    "it; leases are never stolen."
                ) from None
            raise StaleLockError(
                f"lane {self.lane_id} holds a dead lease ({existing.describe()}) "
                f"at {self.path}. Its owner is no longer running, but it is NOT "
                "removed automatically: inspect it, then remove it once you are "
                "sure no process is in that lane. (`unlock` does not touch this "
                "file — it breaks the fleet lock.)"
            ) from None
        try:
            try:
                written = 0
                while written < len(payload):
                    count = os.write(fd, payload[written:])
                    if count <= 0:
                        raise OSError(
                            f"lane lease {self.path}: wrote {written} of "
                            f"{len(payload)} bytes and then made no progress"
                        )
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Ours by construction (the `O_EXCL` above), never written IN FULL,
            # and unreadable if left — see this method's docstring. Best effort
            # on the unlink itself: the caller is already being told the
            # acquisition failed, and a second error about the cleanup would
            # replace the diagnosis with the tidying.
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        self._owned = True
        return self

    def release(self) -> None:
        """Leave the lane, removing the lease only while it is still ours.

        The run-id check is `LoopLock.release`'s, for its reason: a lease that
        somebody else has since recovered must not be deleted by the process
        that used to hold it. A lease that has become UNREADABLE while we held
        it is left on disk — we can no longer prove it is ours, and the honest
        outcome is a lane that refuses entry until a person looks, which is the
        same direction `read` fails in.
        """
        if not self._owned:
            return
        try:
            current = self.read()
        except StateCorruptError:
            current = None
        if current is not None and current.run_id == self.run_id:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._owned = False

    def __enter__(self) -> "LaneLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    # ---- explicit recovery --------------------------------------------------

    def break_stale(self) -> LaneLeaseInfo:
        """Remove a verifiably-dead lease. Refuses a live one, and refuses an
        unreadable one.

        The counterpart to `LoopLock.break_stale`, and the primitive the fleet
        supervisor's lane recovery (candidate 7 of the split plan) is meant to
        call — from inside the fleet lock, which is what makes the
        check-then-act safe there and is why `acquire` will not do it for
        itself. An unreadable lease is refused rather than cleared because
        "remove what you cannot read" is how a lane with a live process in it
        gets opened.
        """
        info = self.read()
        if info is None:
            raise StaleLockError(
                f"no lease at {self.path} — nothing to recover in lane "
                f"{self.lane_id}"
            )
        if self.is_live(info):
            raise LockHeldError(
                f"refusing to remove a LIVE lane lease ({info.describe()}). "
                "Stop that process instead."
            )
        self.path.unlink()
        return info
