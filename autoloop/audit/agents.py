"""Subagent invocation via the Claude Code CLI — shared by the audit and the
implement executors.

The environment's real delegation facility is the `claude` CLI in headless
mode (`claude -p <prompt> --output-format json`) — no model API is used.
`--permission-mode dontAsk` denies anything that would prompt in headless
mode.

**Tool set is a constructor parameter, not a fixed constant (since the
implement executor landed).** `ClaudeCliRunner.__init__` takes
`allowed_tools`/`disallowed_tools`, defaulting to `READ_ONLY_ALLOWED_TOOLS`
(Read/Grep/Glob) / `DISALLOWED_TOOLS` (every editing/executing tool) — every
existing caller (the audit executor, `test_audit_agents.py`) omits both and
gets the exact same read-only argv as before this became configurable.
`autoloop/implement_executor.py`'s `implement_agent_runner` is the OTHER
construction site: it passes a write-capable set (Read/Grep/Glob/Edit/Write)
so its subagent can produce a change. `Bash` and `Task`/`Agent` stay
disallowed on BOTH paths — the executor (not the agent) runs validation and
commits, and a subagent spawning nested agents is out of scope for either
phase; that is what "no uncontrolled nested delegation" means mechanically,
independent of which tool set is otherwise in force.

**Two ways to bound a run, chosen by whether progress can be OBSERVED.**
A write-capable agent runs against a worker repository whose changes are a
direct, first-hand signal that it is still working, so it is supervised by
`stall.py`'s progress detector: spawn, watch the tree, kill only on silence
(or on the absolute ceiling). A read-only audit agent produces no filesystem
change at all, so no such signal exists for it and elapsed time remains the
only bound available — which is also the RIGHT bound there, because a timeout
on a read-only agent costs a re-run and never destroys work. Passing
`progress_probe` is what selects the supervised path; every caller that omits
it keeps the exact `subprocess.run(..., timeout=...)` behaviour it had before
this existed. See `stall.py`'s module docstring for the six measured losses
that motivated the split.

**The ACTION LOG is an optional third thing, and it is default off.**
`action_log_dir=` gives a runner somewhere to append the agent PROCESS's own
output while the run is still going, so an operator can watch a round instead
of waiting for it. `None` — the default, and what every caller that says
nothing gets — opens no file, writes nothing, prints nothing, and returns a
result byte for byte identical to the one this runner produced before the
parameter existed. See `ActionLogWriter` for what the file does and does not
contain, and why nothing in it may fail a round.

**DEFAULT OFF IS NOT THE SAME AS UNWIRED, and the difference is a CALL SITE
rather than anything in this module.** A mechanism no production caller ever
reaches is inert whatever the operator's flag says. So the value is PASSED,
explicitly, from the one place that has already read the operator's config:
`cli._build_executor` computes `config.action_log_dir if
config.audit.action_log else None` once and hands it to
`implement_executor.implement_agent_runner`, which forwards it here. The
write-capable runner a real round runs therefore streams when the operator
asked for it and is untouched when they did not — and none of that depends on
which config a process loaded last, because no process-wide value is involved.

**THE READ-ONLY AUDIT RUNNERS ARE OFF BY CONSTRUCTION, not by a rule in here.**
`cli._build_executor` passes them no directory, so they take the `None`
default. That is deliberate rather than incidental: they are
`subprocess.run(capture_output=True)`, so their output does not exist until
the process has exited, and a file opened for them could only ever be written
at the end — a log that looks like a live stream and is not. A caller that
names a directory for an unsupervised runner anyway still gets one, and the
file SAYS it was buffered (`ACTION_LOG_BUFFERED_NOTICE`) instead of leaving a
reader to assume otherwise.

Tests never invoke the real CLI — AgentRunner is a protocol; the executors
are exercised with fakes, and ClaudeCliRunner itself is tested with a
stubbed subprocess runner (unsupervised path) or a fake spawn + fake clock
(supervised path).
"""

from __future__ import annotations

import itertools
import json
import os
import string
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..stall import (
    ProgressProbe,
    StallPolicy,
    StallReport,
    spawn_supervised,
    supervise,
)
from ..validation_env import strip_validation_vars

READ_ONLY_ALLOWED_TOOLS = ("Read", "Grep", "Glob")
DISALLOWED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash",
    "Task",
    "Agent",
    "WebFetch",
    "WebSearch",
)


# ---- the action log ---------------------------------------------------------
#
# WHAT IT IS: everything the agent PROCESS writes to stdout and stderr, appended
# to one file per round AS IT IS PRODUCED. WHAT IT IS NOT: the model's
# reasoning. Nothing here is a thinking stream and no surface that shows this
# file may call it one — it is process output, which is why every name in this
# section says "action" or "output" and none of them says "thinking".
#
# THAT RULE IS ENFORCED MECHANICALLY, which is why the header written INTO the
# file makes the same point without using either word: `test_agent_action_log
# .py` greps the log's own bytes, its file name and its directory for
# "thinking"/"reasoning"/"chain of thought", and a disclaimer spelling them out
# would defeat a check that is worth more than the phrasing.
#
# THE ONE RULE THE WHOLE SECTION IS BUILT AROUND: observability may never stop
# work. Every function below is total — a directory that cannot be created, a
# handle that refuses a write, a platform without `os.pread` — and the round
# runs exactly as it would have with the log switched off. The failure is
# ANNOUNCED (`_announce`) rather than swallowed, because a log that silently
# stops recording is worth less than no log at all.


#: How many bytes of AGENT OUTPUT one action log may hold.
#:
#: A DISK BOUND, not a usefulness bound: a runaway agent printing in a loop must
#: not be able to fill the volume the loop keeps its state on. Eight mebibytes is
#: far above any round observed here and small enough that a hundred abandoned
#: logs are still under a gigabyte.
#:
#: It bounds the agent's output, not the file: the header and the truncation
#: notice sit on top of it, and so does one short `[stdout]`/`[stderr]` marker
#: per drained chunk. Those markers are bounded too — the pump drains at most
#: once per supervisor tick, so their total is set by the absolute ceiling
#: divided by the poll interval, not by how much the agent prints.
DEFAULT_ACTION_LOG_MAX_BYTES = 8 * 1024 * 1024

#: How much one `os.pread` asks for. Only a buffer size — the pump loops until
#: the file has nothing more to give.
_ACTION_LOG_READ_CHUNK = 65536

#: Said IN the file, at the point the cut happens, and never anywhere else. A
#: capped log that ends silently reads exactly like a round that finished, which
#: is worse than a short log: it is a short log that lies.
ACTION_LOG_TRUNCATION_NOTICE = (
    "\n[TRUNCATED — this action log reached its cap of {cap} bytes of agent "
    "output. The agent kept running; nothing it produced after this point is "
    "recorded in this file.]\n"
)

#: Said IN the file when the run was BUFFERED rather than streamed — the
#: unsupervised path, which is `subprocess.run(capture_output=True)` and hands
#: its output over in one piece when the process has already exited.
#:
#: Written rather than left to be inferred: a file that appeared at the end of a
#: run, with no note, is indistinguishable from a stream that recorded nothing
#: until the last moment. That inference is the fail-open this line closes.
#:
#: REACHED ONLY BY A CALLER THAT NAMED A DIRECTORY. `action_log_dir` defaults to
#: `None` and `cli._build_executor` hands the audit runners nothing, so a
#: buffered log exists only where something asked for one by name — and can
#: therefore be told what it is.
ACTION_LOG_BUFFERED_NOTICE = (
    "\n[NOT STREAMED — this run was bounded by an elapsed timeout, so its "
    "output was buffered by the process runner and everything below was "
    "written when the run ENDED, not as it was produced.]\n"
)

ACTION_LOG_HEADER = """\
# autoloop action log — {domain}
# opened {opened} (UTC), loop pid {pid}
#
# WHAT THIS IS: everything the agent process wrote to stdout and stderr,
# appended as it was produced — the agent's ACTIONS as its CLI reported them.
# WHAT IT IS NOT: any account of how the model decided anything. Read it as a
# record of what was DONE. (The words this paragraph avoids are avoided on
# purpose; see `agents.py`, which says why in full.)
#
# HOW MUCH ARRIVES WHILE THE RUN IS LIVE is the CLI's decision, not this loop's:
# stderr arrives throughout, and `--output-format json` (what this loop asks
# for) holds stdout back until the process exits, so the stdout half lands at
# the end. Each block below is preceded by the stream it came from.
#
# CAP: {cap} bytes of agent output. Reaching it is recorded here, in place.
"""

_SLUG_SAFE = frozenset(string.ascii_letters + string.digits + "._-")


def _describe_exc(exc: BaseException) -> str:
    """`TypeName: detail`, and never the empty string — a blank cause reads as
    no cause at all. Same shape as the one `run` builds for `AgentResult.error`
    and as `stall._describe`, for the same reason."""
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def action_log_slug(name: str) -> str:
    """A file-name component that cannot leave the log directory.

    `spec.domain` is a task id on the implement path and a charter slug on the
    audit path, and both are validated elsewhere — but "validated elsewhere" is
    how a path component becomes a traversal. Everything outside
    `[A-Za-z0-9._-]` becomes `-`, leading and trailing `.`/`-` go (so `../..`
    cannot survive as a name at all), and a value with nothing left is `agent`
    rather than an empty component."""
    cleaned = "".join(ch if ch in _SLUG_SAFE else "-" for ch in str(name))
    return (cleaned.strip(".-") or "agent")[:80]


#: Distinguishes two rounds begun inside one process. `next()` on an
#: `itertools.count` is atomic in CPython, which matters because the audit
#: executor fans its runners out across a thread pool.
_ROUND_SEQUENCE = itertools.count()


def action_log_round_stamp(pid: int | None = None) -> str:
    """What makes one round's log distinct from the next round's.

    Named by TASK and ROUND is the requirement; the runner is handed the task
    (`spec.domain`) and is never told which round it is, so the round half is
    the instant this runner was constructed, the loop's pid, and a counter.
    One runner is one round — `ImplementExecutor._bindings_for` calls the
    configured factory once per `execute()` — so that identifies a round
    exactly, and two rounds of the same task cannot land on one file.

    The COUNTER is not decoration: `%f` is microseconds, and two runners built
    back to back inside one process can land in the same one. A stamp that is
    unique "almost always" would silently merge two rounds' logs on the day it
    was not."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    return f"{stamp}-{pid if pid is not None else os.getpid()}-{next(_ROUND_SEQUENCE)}"


class ActionLogWriter:
    """One round's action log. Bounded, append-only, and unable to fail a round.

    Every public method is TOTAL: it swallows its own failure, remembers the
    first one in `problem`, and stops writing rather than raising. That is not
    defensive style, it is the requirement — this object sits inside the
    write-capable execution path, and an exception escaping it would turn "the
    log could not be opened" into "the agent did not run".

    `active` is False for a writer that never opened (the flag is off, or the
    open failed). Every method on an inactive writer is a no-op, so the caller
    never branches on whether logging is on.
    """

    def __init__(self, handle, path: Path | None, max_bytes: int):
        self._handle = handle
        self.path = path
        self._max_bytes = max(0, int(max_bytes))
        self._written = 0
        self._last_stream = ""
        self.truncated = False
        #: The first thing that went wrong, or `""`. Read by `_announce`; never
        #: put on `AgentResult.error`, which would turn a missing log into a
        #: failed agent.
        self.problem = ""

    @property
    def active(self) -> bool:
        return self._handle is not None

    def note(self, text: str) -> None:
        """The log's OWN prose — header, buffered notice, truncation, the line
        that says a run ended without producing anything. Deliberately not
        counted against the cap: a bound on the agent's output must never be
        able to suppress the sentence explaining that the bound was reached."""
        if self._handle is None:
            return
        try:
            self._emit(text.encode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 — total by contract
            self._disable(exc)

    def write(self, stream: str, data) -> None:
        """Append `data` as output of `stream` ("stdout" / "stderr")."""
        if self._handle is None or not data:
            return
        try:
            if self.truncated:
                return
            raw = data.encode("utf-8", "replace") if isinstance(data, str) else bytes(data)
            if stream != self._last_stream:
                self._emit(f"\n[{stream}]\n".encode())
                self._last_stream = stream
            room = self._max_bytes - self._written
            if len(raw) <= room:
                self._written += len(raw)
                self._emit(raw)
                return
            if room > 0:
                self._written += room
                self._emit(raw[:room])
            self.truncated = True
            self._emit(ACTION_LOG_TRUNCATION_NOTICE.format(cap=self._max_bytes).encode())
        except Exception as exc:  # noqa: BLE001 — total by contract
            self._disable(exc)

    def record_problem(self, exc: BaseException) -> None:
        """Remember a failure that happened NEAR the log rather than in it — the
        pump failing to read the process's output file. The log stays open: one
        unreadable poll is not a reason to stop recording the rest."""
        if not self.problem:
            self.problem = _describe_exc(exc)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except Exception as exc:  # noqa: BLE001 — total by contract
            self.record_problem(exc)

    def _emit(self, raw: bytes) -> None:
        self._handle.write(raw)
        # FLUSHED on every write, and that is the whole feature: a buffered
        # handle would hold the round's output until the process exited, which
        # is exactly the behaviour this log exists to replace.
        self._handle.flush()

    def _disable(self, exc: BaseException) -> None:
        self.record_problem(exc)
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:  # noqa: BLE001 — nothing left to do about it
            pass


#: The writer every caller gets when no `action_log_dir` was configured. Held as
#: ONE shared instance so the off path allocates nothing and touches no
#: filesystem at all.
#:
#: Sharing it is safe because it is never mutated: every method returns
#: immediately while `_handle is None`, and the two that could write to it
#: (`record_problem`, `_disable`) are reached only from the pump and from
#: `_emit`, neither of which exists for an inactive writer. A future caller that
#: wants to record something against a missing log must build its own inert
#: writer — `_open_action_log`'s `except` branch is the pattern.
INACTIVE_ACTION_LOG = ActionLogWriter(None, None, 0)


def open_action_log(
    path: Path, *, max_bytes: int = DEFAULT_ACTION_LOG_MAX_BYTES, opener=open
) -> ActionLogWriter:
    """Open one round's log, or return an INACTIVE writer that remembers why not.

    Never raises. `opener` is a seam for the tests that have to make a write
    fail mid-run; production leaves it at `open`.

    Append mode, not exclusive-create: a round can call `run()` more than once
    (the advisory validation rendezvous re-runs the agent), and those runs are
    one round and belong in one file.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = opener(str(path), "ab")
    except Exception as exc:  # noqa: BLE001 — total by contract
        inert = ActionLogWriter(None, path, max_bytes)
        inert.problem = _describe_exc(exc)
        return inert
    return ActionLogWriter(handle, path, max_bytes)


class _OutputPump:
    """Copies what the process's output files have GAINED into the action log.

    `os.pread`, never `seek`+`read`, and that is a correctness requirement
    rather than a preference. `subprocess.Popen(stdout=<file>)` gives the child
    a `dup2` of the parent's descriptor, and duplicated descriptors SHARE one
    file offset — so a parent that seeks in order to read moves the offset the
    child is about to write at, and the agent's own output starts overwriting
    itself. `os.pread` reads at an absolute position and leaves the shared
    offset alone, which is also what keeps `_run_supervised`'s final
    `seek(0)`/`read()` byte-identical to the buffered behaviour.

    A platform without `os.pread` gets no stream and SAYS so, rather than a log
    that is quietly always empty.
    """

    def __init__(self, log: ActionLogWriter, streams):
        self._log = log
        self._streams = [[name, fileobj, 0] for name, fileobj in streams]
        self._pread = getattr(os, "pread", None)
        if self._pread is None:
            self._log.note(
                "\n[NOT STREAMED — this platform has no os.pread, so the agent's "
                "output could not be read while the run was in progress.]\n"
            )
            self._log.record_problem(
                RuntimeError("os.pread is unavailable; the action log was not streamed")
            )

    def drain(self) -> None:
        """Everything produced since the last call. Never raises, and does no
        unbounded work once the log is full."""
        if self._pread is None or self._log.truncated or not self._log.active:
            return
        for entry in self._streams:
            name, fileobj, offset = entry
            try:
                fd = fileobj.fileno()
                while True:
                    chunk = self._pread(fd, _ACTION_LOG_READ_CHUNK, offset)
                    if not chunk:
                        break
                    offset += len(chunk)
                    entry[2] = offset
                    self._log.write(name, chunk)
                    if self._log.truncated or len(chunk) < _ACTION_LOG_READ_CHUNK:
                        break
            except Exception as exc:  # noqa: BLE001 — total by contract
                self._log.record_problem(exc)


def _pumping_sleep(pump: "_OutputPump", sleep):
    """`sleep`, with the pump run first.

    THE seam that makes the copy happen DURING the run: `supervise` calls its
    `sleep` once per tick while the process is alive, so wrapping it is how the
    log gains bytes before the agent exits rather than after. Returned as a new
    callable rather than applied in place, so with no log `supervise` is handed
    `self._sleep` itself and the off path is unchanged down to the object."""

    def _sleep(seconds):
        pump.drain()
        sleep(seconds)

    return _sleep


def _announce(text: str) -> None:
    """One line on stderr, where the loop's other operator-facing notices go.

    The log is off by default, so this adds nothing to a deployment that has not
    asked for it — and a deployment that HAS asked needs to be told where to
    look, and needs to be told when the answer is "nowhere"."""
    print(text, file=sys.stderr)


@dataclass(frozen=True)
class AgentSpec:
    domain: str  # slug, e.g. "security_paths"
    title: str
    prompt: str
    #: Model alias for this domain ("haiku" / "sonnet" / "opus"). Empty means
    #: "whatever the CLI defaults to". Routing is per domain so mechanical
    #: inventory work does not run on an expensive model — see the allocation
    #: in `executor.DEFAULT_DOMAINS`.
    model: str = ""


@dataclass(frozen=True)
class AgentResult:
    domain: str
    raw_text: str
    returncode: int
    duration_seconds: float
    command: tuple[str, ...]
    error: str = ""
    #: Present ONLY when the supervisor killed this run — a stall or the
    #: absolute ceiling. Defaulted, so every existing construction site (and
    #: every test fake) is unaffected. `error` already carries the same story
    #: as prose; this is the structured form the executor uses to report the
    #: partial-work numbers without re-deriving them.
    stall: StallReport | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.error


class AgentRunner(Protocol):
    def run(self, spec: AgentSpec) -> AgentResult: ...


class ClaudeCliRunner:
    def __init__(
        self,
        repo_root: Path,
        command: tuple[str, ...] = ("claude",),
        timeout_seconds: float = 900.0,
        runner=None,
        allowed_tools: tuple[str, ...] = READ_ONLY_ALLOWED_TOOLS,
        disallowed_tools: tuple[str, ...] = DISALLOWED_TOOLS,
        progress_probe: ProgressProbe | None = None,
        stall_policy: StallPolicy | None = None,
        spawn=None,
        clock=time.monotonic,
        sleep=time.sleep,
        action_log_dir: Path | None = None,
        action_log_max_bytes: int = DEFAULT_ACTION_LOG_MAX_BYTES,
        action_log_opener=open,
    ):
        """`allowed_tools`/`disallowed_tools` default to the read-only audit
        set — every caller that does not pass them (every existing one)
        builds the exact same argv as before these became parameters. Pass a
        different pair (see `implement_executor.implement_agent_runner`) to
        run a write-capable subagent instead.

        `progress_probe` is what selects HOW the run is bounded, and it is
        the only switch:

        * absent (every audit caller) — `subprocess.run(..., timeout=
          timeout_seconds)`, byte for byte the behaviour that existed before
          the stall detector. `timeout_seconds` is an ELAPSED bound and means
          exactly what it always meant.
        * present (the write-capable implement runner) — spawn and supervise
          against `stall_policy`: killed on SILENCE in the worker repository,
          not on elapsed time, with `stall_policy.ceiling_seconds` as the
          absolute backstop. `timeout_seconds` is then unused, deliberately:
          two live time bounds on one run is how a "progress-based" detector
          quietly goes back to being a timeout.

        `spawn`/`clock`/`sleep` exist so the supervised path is testable with
        no real process and no real waiting; production leaves all three at
        their defaults.

        **`action_log_dir` is a plain `Path | None`, and `None` is the
        default.** A directory means this runner appends the agent process's
        output there; `None` means it logs nowhere. There is no third state and
        no value resolved from anywhere else in the process — WHERE the answer
        comes from is the caller's business, and in production the caller is
        `cli._build_executor`, which passes `config.action_log_dir if
        config.audit.action_log else None` into
        `implement_executor.implement_agent_runner` for the write-capable
        runner and passes the read-only audit runners nothing at all.

        That is the whole of the production wiring, and it is deliberately a
        parameter rather than a process-wide default: a default set as a side
        effect of loading a config makes every runner's behaviour depend on
        which config the process read last, which is not something a reader of
        this call site could predict.

        The audit runners being handed nothing is not an accident of the call
        site either. They are `subprocess.run(capture_output=True)`, so their
        output does not exist until the process has exited and a file opened
        for them could only ever be written at the end. A caller that names one
        anyway still gets it, and the file records
        `ACTION_LOG_BUFFERED_NOTICE` saying exactly what it is.

        With the log off — `None`, the default, and what every caller that says
        nothing gets — no directory is created, no file is opened, nothing is
        printed, `sleep` is passed to `supervise` unwrapped, and the returned
        `AgentResult` is byte for byte what it was before this parameter
        existed. On means one file per round under that directory, appended to
        while the agent is still running. `action_log_max_bytes` caps the AGENT
        OUTPUT it records (see `DEFAULT_ACTION_LOG_MAX_BYTES`);
        `action_log_opener` is a test seam.

        ONE RUNNER IS ONE ROUND, which is what makes the file name honest:
        `ImplementExecutor._bindings_for` builds a fresh runner from the
        configured factory on every `execute()`, so the round stamp fixed here
        identifies that round and the 1..N `run()` calls inside it append to the
        same file.
        """
        self._repo_root = Path(repo_root)
        self._command = tuple(command)
        self._timeout = timeout_seconds
        self._runner = runner or subprocess.run
        self._allowed_tools = tuple(allowed_tools)
        self._disallowed_tools = tuple(disallowed_tools)
        self._progress_probe = progress_probe
        self._stall_policy = stall_policy or StallPolicy()
        self._spawn = spawn or spawn_supervised
        self._clock = clock
        self._sleep = sleep
        # RESOLVED ONCE, here, and never re-read: whatever the caller passed is
        # this round's answer for the whole of its length.
        #
        # TOTAL, like everything else in this section, and for the reason the
        # section header gives: this sits in the write-capable execution path,
        # so a value that cannot name a directory must cost the round the LOG
        # and nothing else. It is announced rather than swallowed — a log that
        # is quietly off is the fail-open this whole feature would die of.
        if action_log_dir is None:
            resolved_log_dir = None
        else:
            try:
                resolved_log_dir = Path(action_log_dir)
            except Exception as exc:  # noqa: BLE001 — total by contract
                _announce(
                    f"autoloop: {action_log_dir!r} cannot name an agent action "
                    f"log directory ({_describe_exc(exc)}); the action log is "
                    "off and the round is otherwise unaffected"
                )
                resolved_log_dir = None
        self._action_log_dir = resolved_log_dir
        self._action_log_max_bytes = action_log_max_bytes
        self._action_log_opener = action_log_opener
        self._action_log_stamp = (
            action_log_round_stamp() if resolved_log_dir is not None else ""
        )
        self._action_log_announced = False

    def build_argv(self, spec: AgentSpec) -> list[str]:
        model_flag = ["--model", spec.model] if spec.model else []
        return [
            *self._command,
            "-p",
            spec.prompt,
            *model_flag,
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            *self._allowed_tools,
            "--disallowedTools",
            *self._disallowed_tools,
        ]

    def run(self, spec: AgentSpec) -> AgentResult:
        """Never raises. Every failure — expected or not — comes back as an
        `AgentResult` carrying the cause.

        `_run_agents` fans these out through `list(pool.map(...))`, so ONE
        escaping exception discards the whole batch, including the domains
        that already finished. One agent falling over is a single coverage
        gap (the executor turns `not result.ok` into an `agent_failures`
        entry); it is not a reason to lose an audit run. The whole body is
        guarded, not just the subprocess call — building the argv, reading
        `proc.stdout` / `proc.returncode` and decoding the output are all part
        of the same failure surface.

        The action log is opened BEFORE that guard and closed in a `finally`
        after it, because neither opening it nor failing to open it may change
        what this method returns — see `_open_action_log`."""
        started = time.monotonic()
        log = self._open_action_log(spec)
        try:
            return self._run_guarded(spec, started, log)
        finally:
            log.close()
            self._announce_problem(log)

    def _run_guarded(self, spec: AgentSpec, started: float, log: ActionLogWriter) -> AgentResult:
        """`run`'s body, split out only so the log's `finally` in `run` has
        something to wrap. Everything below is exactly what `run` did before the
        action log existed."""
        # Bound BEFORE the try, so reporting a failure can never itself fail.
        # `build_argv` reads `spec.model` and the configured tool tuples and is
        # therefore inside the guard — but `_failed` puts `argv` in the result,
        # so an unbound name there would turn a caught exception back into an
        # escaping one, in the exact handler that exists to stop that. The
        # fallback is the base command: enough to say WHAT was being run.
        # (`spec.domain` is read the same way and is not similarly guarded —
        # whatever it were captured into would need a guard of its own.)
        argv: list[str] = list(self._command)
        # EXPLICIT removal, not merely a failure to add: a subagent inherits
        # the loop's environment by construction, so the validation database
        # credentials have to be taken back out for the boundary in
        # `validation_env.py` to mean anything. Applied to BOTH tool sets —
        # the write-capable implement runner is the one the brief names, but
        # a read-only audit subagent has no business seeing them either, and
        # one unconditional strip cannot be forgotten at a future call site.
        # `strip_validation_vars` also drops any `*VALIDATION_ENV_FILE*`
        # variable, so the agent never learns where the file lives.
        try:
            argv = self.build_argv(spec)
            if self._progress_probe is None:
                proc = self._runner(
                    argv,
                    cwd=str(self._repo_root),
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    env=strip_validation_vars(),
                )
                stdout, stderr = proc.stdout or "", proc.stderr
                returncode, stall = proc.returncode, None
                # `capture_output=True` above is untouched, so the result stays
                # byte-identical; what the log gets on this path is therefore
                # the buffered output, and the file SAYS that rather than
                # letting a reader assume it was watching a stream.
                if log.active:
                    log.note(ACTION_LOG_BUFFERED_NOTICE)
                    log.write("stdout", stdout)
                    log.write("stderr", stderr or "")
            else:
                stdout, stderr, returncode, stall = self._run_supervised(argv, log)
            text = _extract_result_text(stdout)
            error = ""
            if stall is not None:
                # The stall report IS the cause — it already names the silence,
                # the elapsed time and the partial work. Whatever the killed
                # process left on stderr is a consequence of the kill, not the
                # reason for it, so it must not be reported as one.
                error = stall.describe()
            elif returncode != 0:
                error = summarize_failure(stderr, stdout, returncode)
            return AgentResult(
                domain=spec.domain,
                raw_text=text,
                returncode=returncode,
                duration_seconds=time.monotonic() - started,
                command=tuple(argv),
                error=error,
                stall=stall,
            )
        except subprocess.TimeoutExpired:
            return self._failed(
                spec, argv, started, f"agent timed out after {self._timeout}s", log
            )
        # BEFORE the broad clause below, and it must stay there:
        # FileNotFoundError is an OSError subclass, so a broad `except
        # Exception` placed above would swallow the one message that tells an
        # operator the `claude` binary is missing rather than misbehaving.
        except FileNotFoundError as exc:
            return self._failed(
                spec, argv, started, f"agent command not found: {exc}", log
            )
        except Exception as exc:  # noqa: BLE001 — deliberately total; see the docstring
            # The TYPE NAME is not decoration. `str(exc)` is empty for a bare
            # `MemoryError`/`RuntimeError()`, and an AgentResult whose `error`
            # is "" reads as `ok` (see `AgentResult.ok`) — a domain that blew
            # up would be counted as covered with zero findings. The message
            # is non-empty unconditionally.
            detail = str(exc).strip()
            described = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            return self._failed(spec, argv, started, f"agent raised {described}", log)

    # ---- the action log ------------------------------------------------------

    def _open_action_log(self, spec: AgentSpec) -> ActionLogWriter:
        """This round's log, or the inactive one. NEVER raises, and never lets
        the flag being on cost the round anything it would not otherwise pay."""
        if self._action_log_dir is None:
            return INACTIVE_ACTION_LOG
        try:
            path = self._action_log_dir / (
                f"{action_log_slug(spec.domain)}-{self._action_log_stamp}.log"
            )
            log = open_action_log(
                path,
                max_bytes=self._action_log_max_bytes,
                opener=self._action_log_opener,
            )
            if log.active:
                log.note(
                    ACTION_LOG_HEADER.format(
                        domain=spec.domain,
                        opened=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        pid=os.getpid(),
                        cap=self._action_log_max_bytes,
                    )
                )
            if not self._action_log_announced and log.active:
                self._action_log_announced = True
                _announce(f"autoloop: streaming the agent action log to {path}")
            return log
        except Exception as exc:  # noqa: BLE001 — total by contract
            # `spec.domain` is read above and could be anything; a runner that
            # raised here would fail a round over a file it was only offering.
            inert = ActionLogWriter(None, None, self._action_log_max_bytes)
            inert.problem = _describe_exc(exc)
            return inert

    def _announce_problem(self, log: ActionLogWriter) -> None:
        if self._action_log_dir is None or not log.problem:
            return
        where = log.path if log.path is not None else self._action_log_dir
        _announce(
            f"autoloop: the agent action log at {where} is incomplete or was "
            f"never written ({log.problem}); the round ran normally without it"
        )

    def _run_supervised(
        self, argv: list[str], log: ActionLogWriter = INACTIVE_ACTION_LOG
    ) -> tuple[str, str, int, StallReport | None]:
        """Spawn, watch the worker tree, collect whatever the run produced.

        Output goes to temporary FILES rather than pipes for two reasons, both
        load-bearing: an undrained pipe blocks the child once its OS buffer
        fills — a hang manufactured by the hang detector — and a temp file
        sits outside the worker repository, so the agent's own output can
        never be mistaken for filesystem progress by the probe watching that
        repository. Partial output from a killed run is kept: it is often the
        only account of what the agent was doing when it wedged.

        THE ACTION LOG IS PUMPED FROM INSIDE THE SUPERVISOR LOOP, through the
        `sleep` seam `supervise` already takes, and that is the whole of the
        "observable while it runs" claim: the copy happens once per supervisor
        tick, while the process is still alive, not once at the end. Copying
        these temp files after `supervise` returned would leave an identical
        FILE behind and would still be a round nobody could watch — which is
        precisely the mutation the tests for this exist to kill.

        With no active log the wrapper is not built at all and `self._sleep` is
        handed over unchanged, so the off path is the code that was here before.
        """
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            handle = self._spawn(
                argv,
                cwd=str(self._repo_root),
                env=strip_validation_vars(),
                stdout=out,
                stderr=err,
            )
            pump = (
                _OutputPump(log, (("stdout", out), ("stderr", err)))
                if log.active
                else None
            )
            sleep = self._sleep if pump is None else _pumping_sleep(pump, self._sleep)
            supervision = supervise(
                handle,
                self._progress_probe,
                self._stall_policy,
                clock=self._clock,
                sleep=sleep,
            )
            if pump is not None:
                # The tail: whatever arrived after the last tick, including
                # everything a killed run left behind.
                pump.drain()
            out.seek(0)
            stdout = out.read().decode("utf-8", "replace")
            err.seek(0)
            stderr = err.read().decode("utf-8", "replace")
        returncode = supervision.returncode
        return stdout, stderr, (returncode if returncode is not None else -1), supervision.report

    def _failed(
        self,
        spec: AgentSpec,
        argv: list[str],
        started: float,
        error: str,
        log: ActionLogWriter = INACTIVE_ACTION_LOG,
    ) -> AgentResult:
        # Recorded IN the log too: a file holding only a header, with no note,
        # is indistinguishable from a run that printed nothing — and "the agent
        # never started" is the one thing an operator watching this file most
        # needs it to say.
        log.note(f"\n[the agent run did not complete: {error}]\n")
        return AgentResult(
            domain=spec.domain,
            raw_text="",
            returncode=-1,
            duration_seconds=time.monotonic() - started,
            command=tuple(argv),
            error=error,
        )


#: stderr lines the CLI prints that are ADVISORY, never a cause of failure.
#:
#: The connectors notice is the one that cost real time: the loop's subagents
#: run nested inside a Claude Code session, so they inherit its auth context,
#: the CLI decides "another auth source" is present and disables claude.ai
#: connectors, and it prints that to stderr BEFORE anything else. The old
#: capture took `stderr[:2000]` — the HEAD — so this banner became the entire
#: reported cause of every non-zero exit. It travelled into the executor
#: summary, into the review packet, and out as a directive asking an operator
#: to unset `ANTHROPIC_API_KEY` — a variable that was not set anywhere, while
#: the actual failure was never shown at all.
#:
#: Matched as substrings against stripped lines, case-insensitively. Keep this
#: list SHORT and specific: anything matched here is dropped from the reported
#: cause, so a pattern that is too broad hides real failures — the exact bug
#: this exists to fix.
BENIGN_STDERR_MARKERS: tuple[str, ...] = (
    "claude.ai connectors are disabled",
    "unset it to load your organization's connectors",
)

#: Head and tail kept when output is long. Both ends, because a traceback puts
#: its cause LAST while a banner puts itself first — keeping only one end loses
#: whichever the failure happens to be.
_EXCERPT_SIDE = 900


def _is_benign(line: str) -> bool:
    lowered = line.strip().lower().lstrip("⚠! ").strip()
    return any(marker in lowered for marker in BENIGN_STDERR_MARKERS)


def _excerpt(text: str) -> str:
    text = text.strip()
    if len(text) <= _EXCERPT_SIDE * 2:
        return text
    dropped = len(text) - _EXCERPT_SIDE * 2
    return f"{text[:_EXCERPT_SIDE]}\n… [{dropped} chars elided] …\n{text[-_EXCERPT_SIDE:]}"


def summarize_failure(stderr: str | None, stdout: str | None, returncode: int) -> str:
    """What actually went wrong, with advisory banners demoted rather than
    reported as the cause.

    Returns the substantive output when there is any. When the output is
    NOTHING BUT advisory notices, it says so explicitly instead of presenting
    a warning as the failure — "exited N with no diagnostic output" is a
    worse-sounding but far more honest answer, and it is the one that sends
    someone looking in the right place.
    """
    raw = (stderr or "").strip() or (stdout or "").strip()
    if not raw:
        return f"non-zero exit ({returncode}) with no output on stderr or stdout"

    lines = raw.splitlines()
    substantive = [ln for ln in lines if ln.strip() and not _is_benign(ln)]
    advisory = [ln.strip() for ln in lines if ln.strip() and _is_benign(ln)]

    if substantive:
        summary = _excerpt("\n".join(substantive))
        if advisory:
            # Kept, but clearly separated from the cause and never first.
            summary += f"\n(advisory, not the cause: {advisory[0][:160]})"
        return summary

    return (
        f"non-zero exit ({returncode}) with NO diagnostic output — stderr held "
        f"only advisory notice(s), which are not the cause: {advisory[0][:200]}"
    )


#: Phrases that identify an agent run stopped by the PROVIDER rather than by
#: anything in the repository — a throttle, an exhausted allowance, or the API
#: being unavailable. Deliberately narrow and lowercase-matched: this list is
#: the difference between "the round was destroyed by something nobody could
#: have recovered from" and "the work was wrong", and the second reading is the
#: safe default (see `classify_agent_fault`).
_PROVIDER_FAULT_PHRASES = (
    "rate limit",
    "rate_limit",
    "rate-limit",
    "too many requests",
    "overloaded",
    "usage limit",
    "quota",
    "session limit",
    "insufficient credit",
    "service unavailable",
)

#: HTTP statuses that mean the same thing, paired with a context requirement.
#: A bare "429" is NOT enough on its own: `result.error` is an excerpt of the
#: agent's own output, which can quote a line number, a byte count or a test
#: name. Requiring one of `_PROVIDER_FAULT_CONTEXT` alongside the code keeps a
#: coincidental three-digit number from excusing a genuine failure.
_PROVIDER_FAULT_STATUSES = ("429", "502", "503", "529")
_PROVIDER_FAULT_CONTEXT = ("api", "http", "status", "request", "error code")

#: Reason slugs `classify_agent_fault` can return. They travel to
#: `executor.ExecutionOutcome.fault_kind` and end up in a
#: `worktask.TaskExecution.attempt_ledger` entry, so they are stable strings an
#: operator greps, not prose.
AGENT_FAULT_STALL = "agent_killed_by_supervisor"
AGENT_FAULT_PROVIDER = "agent_provider_unavailable"


def classify_agent_fault(result: "AgentResult") -> str:
    """Which ENVIRONMENTAL fault stopped this agent, or `""` for none.

    Two positively-identified causes, both read from structured signals rather
    than from the summary prose:

      * the supervisor killed the run — `result.stall` is present, which
        `_run_supervised` sets only for a stall or the absolute ceiling;
      * the provider refused to serve it — `result.error` names a throttle, an
        exhausted allowance or an API outage.

    Everything else returns `""` and is charged to the task's own attempt
    budget. That default is the load-bearing part: an agent that exits non-zero
    because it wrote broken code, ran out of context reasoning in circles, or
    simply failed, IS the task's problem, and misreading one of those as a
    fault would remove the bound on a task that fails every single round.

    The one FALSE-POSITIVE direction, stated rather than left implicit: on the
    `not result.ok` path `result.error` is `summarize_failure(stderr, stdout,
    returncode)` — an excerpt of the agent process's own output. An agent
    working on this repository's `services/rate_limiter.py` that exits non-zero
    can therefore put "rate limit" into that excerpt and have a genuine failure
    read as a fault. The consequence is bounded to one charge on the fault
    budget, which still terminates (`MAX_TASK_FAULT_ATTEMPTS`), and the
    `attempt_ledger` entry names the classification so it is visible rather
    than silent. Narrowing the phrases further would trade that for the far
    worse direction — a real 429 charged to a converging task.
    """
    if result.stall is not None:
        return AGENT_FAULT_STALL
    error = (result.error or "").lower()
    if any(phrase in error for phrase in _PROVIDER_FAULT_PHRASES):
        return AGENT_FAULT_PROVIDER
    if any(code in error for code in _PROVIDER_FAULT_STATUSES) and any(
        word in error for word in _PROVIDER_FAULT_CONTEXT
    ):
        return AGENT_FAULT_PROVIDER
    return ""


def _extract_result_text(stdout: str) -> str:
    """`--output-format json` wraps the reply; unwrap `result` when present,
    fall back to the raw stdout otherwise."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        return data["result"]
    return stdout
