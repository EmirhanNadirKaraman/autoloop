"""Email the operator when the loop's status CHANGES, from inside the loop.

Hooked behind `heartbeat.publish`, which is the one chokepoint every status
update already passes through — so a new call site cannot forget to notify.

WHAT THIS OWNS, AND WHAT IT CANNOT. Read `heartbeat`'s module docstring first:
the loop publishes, the monitor judges. Everything here is a SELF-REPORT, so it
covers exactly the statuses the loop KNOWS — `parked`, `blocked`, `paused`, a
clean `stopped`, and task/phase transitions — and covers DEATH and STALENESS
not at all. A loop that has hung, crashed or been killed cannot email about it;
it simply stops writing, and only something outside the process can see that.
`scripts/autoloop_health_notify.sh` is that something, which is why it exists
and why it reads nothing but the heartbeat file. Email arriving is evidence the
loop is alive; email NOT arriving is evidence of nothing.

IT MUST NEVER BREAK A ROUND. `publish` is documented as deliberately tolerant —
it is called from the hot loop and must not need a fully-formed state, a
readable blocker directory, or anything else that could raise mid-round. Sending
mail is a network call, so it is contained by four rules, all load-bearing:

* **Every failure is swallowed and logged, never raised.** `notify_status_change`
  returns an outcome string and has no failing path that escapes it.
* **One hard timeout, no retry.** The transport runs on a daemon thread joined
  for exactly `timeout_seconds`; a send that outlives that is abandoned. This is
  NOT the thread the task forbids — that prohibition is about covering the
  loop's own death from inside it, which nothing here attempts. A thread is
  required rather than preferred: `smtplib`'s socket timeout bounds a socket,
  and the failure being bounded here is "the send call does not return".
* **The change is recorded BEFORE the attempt.** A dead SMTP server therefore
  costs one timeout per status change and nothing per publish; recording on
  success instead would make every subsequent call in the hot loop pay it again.
* **A hung server cannot accumulate sockets.** At most `MAX_ABANDONED_SENDS`
  abandoned sends may be outstanding; past that a send is skipped and logged.

SECRETS. The SMTP password is never read from the config file (which is
tracked). It comes from an environment variable or a file named by the config,
and an absent, blank or unreadable one REFUSES the send rather than falling back
to an unauthenticated one. Every rendered and logged string goes through
`_safe`, which replaces the resolved password and rewrites the operator's home
directory to `~`.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from .heartbeat import BLOCKED, PARKED, PAUSED, RUNNING, STOPPED

#: Every status a heartbeat can carry, and therefore every status
#: `[notify].statuses` may name. Imported from `heartbeat` rather than spelled
#: again: a vocabulary that agreed today would disagree the first time one moves.
NOTIFY_STATUS_VOCABULARY: tuple[str, ...] = (RUNNING, PAUSED, PARKED, BLOCKED, STOPPED)

#: The default `[notify].statuses` — all of them. The task/phase transitions
#: inside `running` are the observed cadence this was sized for (one change
#: every 15-45 minutes); an operator who wants only the ones that need a human
#: narrows the list to `["parked", "blocked", "paused"]`.
NOTIFY_DEFAULT_STATUSES: tuple[str, ...] = NOTIFY_STATUS_VOCABULARY

#: `[notify].tls`. There is deliberately no "none": authentication is mandatory
#: (see `_resolve_credentials`), so a cleartext session would put the password
#: on the wire and make the whole of this module's secret handling theatre.
NOTIFY_TLS_STARTTLS = "starttls"
NOTIFY_TLS_SSL = "ssl"
NOTIFY_TLS_MODES: tuple[str, ...] = (NOTIFY_TLS_STARTTLS, NOTIFY_TLS_SSL)

#: What `_safe` puts where a password was, if one ever reaches a rendered or
#: logged string. Nothing is expected to; this is the structural guarantee.
REDACTED = "***"

#: The longest `detail` an email carries, matching `heartbeat.write`'s own bound
#: so the mail and the beat cannot disagree about what the loop said.
MAX_DETAIL_CHARS = 300

#: The longest Subject. Every part of it is read out of a state file, so it is
#: bounded here rather than trusted to be short.
MAX_SUBJECT_CHARS = 200

#: The send timeout used when a config carries an unusable one. `config.py`
#: refuses a non-positive `[notify].timeout_seconds` at load, so this is reached
#: only by a `NotifyConfig` built in code — and reaching it must not mean
#: "abandon instantly", which would switch notification off after three changes.
DEFAULT_TIMEOUT_SECONDS = 10.0

#: How many abandoned (timed-out) sends may be outstanding before a new one is
#: skipped. Each holds a socket, so an SMTP server that accepts connections and
#: never answers would otherwise leak one per status change for the life of the
#: run.
MAX_ABANDONED_SENDS = 3

# ---- outcomes ---------------------------------------------------------------
#
# `notify_status_change` RETURNS one of these rather than raising anything. They
# exist for the tests and for the log line; nothing branches on them.
OUTCOME_DISABLED = "disabled"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_FILTERED = "filtered"
OUTCOME_REFUSED = "refused"
OUTCOME_BUSY = "busy"
OUTCOME_SENT = "sent"
OUTCOME_FAILED = "failed"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"


@dataclass(frozen=True)
class StatusSnapshot:
    """What the loop knew at one `publish`. Built by `snapshot` below."""

    status: str
    phase: str = ""
    task_id: str = ""
    decision: str = ""
    open_blockers: int = 0
    detail: str = ""
    session_id: str = ""
    ts: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        """THE change tuple: status, phase, current task id, decision.

        Everything else the email carries — the blocker count, the detail, the
        session id, the timestamp — is deliberately outside it. Those move
        without the loop's situation changing, and a tuple including them would
        email on every beat.
        """
        return (self.status, self.phase, self.task_id, self.decision)


@dataclass(frozen=True)
class Envelope:
    """One rendered message plus how to deliver it. The unit a transport takes.

    `password` carries `repr=False` so a traceback or a debug print of this
    object cannot show it. That is one layer; `_safe` is the other, and the one
    that covers strings this dataclass never sees.
    """

    host: str
    port: int
    tls: str
    timeout_seconds: float
    sender: str
    recipient: str
    subject: str
    body: str
    username: str
    password: str = field(default="", repr=False)


#: Last OBSERVED change tuple, per state-file path, for THIS process. Consulted
#: ahead of the file, because the file may be unwritable — without it an
#: unwritable state directory would turn "email on a change" into "email on
#: every publish", which is the flood this whole module is shaped to avoid.
_LAST_OBSERVED: dict[str, tuple[str, str, str, str]] = {}

#: Sends that outlived their timeout and were abandoned. Pruned on every call.
_ABANDONED: list[threading.Thread] = []


def clear_process_cache() -> None:
    """Forget this process's in-memory dedup cache and abandoned sends.

    For tests, which simulate a loop RESTART: a restart is exactly "the process
    cache is gone and only the state file remains", and without this a test
    could not tell the file from the cache.
    """
    _LAST_OBSERVED.clear()
    _ABANDONED.clear()


# ---- rendering --------------------------------------------------------------


def _home() -> str:
    """The operator's home directory, or `""` when it cannot be determined.

    `Path.home()` raises when no home is resolvable, and this is called from the
    hot loop's redaction path — where raising would be the one failure that
    matters.
    """
    try:
        home = str(Path.home())
    except Exception:
        return ""
    return home if home not in ("", "/") else ""


def _safe(text, secret: str = "") -> str:
    """THE choke point every rendered and logged string passes through.

    Two substitutions, both structural rather than incidental: the resolved
    password becomes `***` (so an exception string nobody anticipated cannot
    carry it), and the operator's home directory becomes `~` (so a park detail
    naming a worker repo does not mail out an absolute path from their home).
    """
    out = str(text)
    if secret:
        out = out.replace(secret, REDACTED)
    home = _home()
    if home:
        out = out.replace(home, "~")
    return out


def _one_line(text: str) -> str:
    """A header-safe fragment: no CR, no LF, no other control characters.

    Subject lines are assembled from `status`, `task_id` and `phase`, which come
    from a state file. A newline in any of them is header injection, so they are
    flattened rather than trusted.
    """
    return "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in str(text))


def render_subject(snapshot: StatusSnapshot, secret: str = "") -> str:
    """Status, task and phase — the identity of the change at a glance.

    Bounded, because every part of it comes from a state file: a header of
    unbounded length is a malformed message rather than a long subject.
    """
    return _one_line(
        _safe(
            f"[autoloop] {snapshot.status or '-'} | task "
            f"{snapshot.task_id or '-'} | phase {snapshot.phase or '-'}",
            secret,
        )
    )[:MAX_SUBJECT_CHARS]


def render_body(snapshot: StatusSnapshot, secret: str = "") -> str:
    """What an operator would otherwise run `status` to learn. Plain text.

    Carries no secret and no absolute path out of the operator's home — see
    `_safe` — and states the limitation in the mail itself, because the person
    reading it at 3am is exactly the one who must not conclude that silence
    means health.
    """
    lines = [
        "autoloop status change",
        "",
        f"status:        {snapshot.status or '-'}",
        f"phase:         {snapshot.phase or '-'}",
        f"task:          {snapshot.task_id or '-'}",
        f"decision:      {snapshot.decision or '-'}",
        f"open blockers: {snapshot.open_blockers}",
        f"session:       {snapshot.session_id or '-'}",
        f"time:          {snapshot.ts or '-'}",
        "",
        "detail:",
        snapshot.detail or "(none)",
        "",
        "Sent by the loop itself, from heartbeat.publish. It reports only what",
        "the loop KNOWS: a park, a block, a pause, a clean stop, a task or",
        "phase transition. It CANNOT report that the loop has hung, crashed or",
        "been killed — that loop simply stops writing its heartbeat, and only",
        "the external monitor reading that file can see it. Mail arriving means",
        "the loop is alive; mail not arriving means nothing.",
    ]
    return _safe("\n".join(lines) + "\n", secret)


# ---- the change tuple, and where it is remembered ---------------------------


def snapshot(
    state=None,
    *,
    status: str,
    phase: str = "",
    session_id: str = "",
    open_blockers: int = 0,
    detail: str = "",
    now: datetime | None = None,
) -> StatusSnapshot:
    """Build a `StatusSnapshot` from whatever the caller already has in hand.

    As tolerant as `publish` is, and for its reason: `state` may be `None`, may
    be a half-written object, and `current_task` may be anything a hand-edited
    state file contains. Nothing here raises.
    """
    current = getattr(state, "current_task", None)
    if not isinstance(current, dict):
        current = {}
    task_id = str(current.get("task_id") or "")
    # The decision that produced the state the loop is in: the dispatched task's
    # own when a task is in flight, and the session's last otherwise — so a
    # `parked` beat between tasks still names what was decided.
    decision = str(current.get("decision") or "")
    if not decision:
        decision = str(getattr(state, "last_decision", "") or "")
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return StatusSnapshot(
        status=str(status or ""),
        phase=str(phase or ""),
        task_id=task_id,
        decision=decision,
        open_blockers=int(open_blockers or 0),
        detail=str(detail or "")[:MAX_DETAIL_CHARS],
        session_id=str(session_id or ""),
        ts=stamp,
    )


def _state_path(config) -> Path | None:
    """Where the last observed tuple is persisted — beside the heartbeat.

    `None` when the config cannot answer, which is not fatal: the process cache
    still suppresses a repeat within this run.
    """
    try:
        return Path(config.notify_state_file)
    except Exception:
        return None


def _read_last(path: Path | None) -> tuple[str, str, str, str] | None:
    """The tuple last observed, or `None` when there is nothing usable.

    `None` is the SEND direction, deliberately: a missing, unreadable or
    corrupt record means the loop cannot prove it has already reported this
    state, and one duplicate email is a far better failure than a park nobody
    is told about.
    """
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    try:
        return (
            str(record.get("status") or ""),
            str(record.get("phase") or ""),
            str(record.get("task_id") or ""),
            str(record.get("decision") or ""),
        )
    except Exception:
        return None


def _write_last(path: Path | None, snap: StatusSnapshot) -> bool:
    """Persist the observed tuple atomically. `False` when it could not be."""
    if path is None:
        return False
    payload = {
        "status": snap.status,
        "phase": snap.phase,
        "task_id": snap.task_id,
        "decision": snap.decision,
        "ts": snap.ts,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ---- credentials ------------------------------------------------------------


def _read_password_file(raw_path: str) -> str:
    """The first non-blank line of the named file, stripped.

    A LINE rather than the whole file: a trailing newline is what an editor
    leaves behind, and stripping the whole text would carry the newlines of a
    multi-line file into the credential instead of failing cleanly. `""` for
    anything unusable — missing, unreadable, a directory, blank — because those
    all mean the same thing to the one caller, and it refuses on all of them.
    """
    try:
        text = Path(raw_path).expanduser().read_text(encoding="utf-8")
    except Exception:
        return ""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _resolve_credentials(cfg) -> tuple[str, str, str]:
    """`(username, password, refusal)` — and `refusal` is non-empty ONLY when
    the send must not happen.

    Every "absent" is treated the same way, because they fail the same way: an
    unset variable, a variable set to `""`, a file that is missing, unreadable
    or blank all mean there is no password. None of them may fall through to an
    unauthenticated send — that is the guard which would otherwise switch itself
    off exactly when the credential is broken.

    The refusal text never contains the password, and never could: it is only
    reached when there isn't one.
    """
    username = str(getattr(cfg, "username", "") or "").strip()
    if not username:
        return "", "", (
            "no notify.username is configured, and an unauthenticated send is "
            "not a fallback this loop takes"
        )
    env_name = str(getattr(cfg, "password_env", "") or "").strip()
    file_name = str(getattr(cfg, "password_file", "") or "").strip()
    if env_name and file_name:
        return "", "", (
            "notify.password_env and notify.password_file are both set — set "
            "exactly one, so there is no question which secret was used"
        )
    if env_name:
        password = str(os.environ.get(env_name, "") or "").strip()
        if not password:
            return "", "", (
                f"the environment variable named by notify.password_env "
                f"({env_name}) is unset or empty in the loop's environment"
            )
        return username, password, ""
    if file_name:
        password = _read_password_file(file_name)
        if not password:
            return "", "", (
                "the file named by notify.password_file is missing, unreadable "
                "or blank"
            )
        return username, password, ""
    return "", "", (
        "neither notify.password_env nor notify.password_file is set, so there "
        "is no password to authenticate with"
    )


# ---- transport --------------------------------------------------------------


def smtp_transport(envelope: Envelope) -> None:
    """The real one. Injected over in every test; no test opens a socket.

    `timeout=` is passed to `smtplib` as well as being bounded by the caller's
    join: this one bounds each socket operation, that one bounds the call.
    """
    message = EmailMessage()
    message["From"] = envelope.sender
    message["To"] = envelope.recipient
    message["Subject"] = envelope.subject
    message.set_content(envelope.body)
    context = ssl.create_default_context()
    if envelope.tls == NOTIFY_TLS_SSL:
        client = smtplib.SMTP_SSL(
            envelope.host, envelope.port, timeout=envelope.timeout_seconds,
            context=context,
        )
    else:
        client = smtplib.SMTP(
            envelope.host, envelope.port, timeout=envelope.timeout_seconds
        )
    with client:
        if envelope.tls == NOTIFY_TLS_STARTTLS:
            client.starttls(context=context)
        client.login(envelope.username, envelope.password)
        client.send_message(message)


def _log_stderr(text: str) -> None:
    """Where a swallowed failure goes. `cli.py`'s notices use the same shape."""
    print(f"autoloop: notify — {text}", file=sys.stderr)


def _prune_abandoned() -> int:
    """Drop finished abandoned sends; return how many are still outstanding."""
    _ABANDONED[:] = [thread for thread in _ABANDONED if thread.is_alive()]
    return len(_ABANDONED)


def _deliver(transport, envelope: Envelope, secret: str, log) -> str:
    """One attempt, bounded, never retried, and unable to raise.

    The daemon thread is the bound: a transport that never returns is left
    behind rather than waited on, so the round pays `timeout_seconds` and not a
    second more. It covers no failure of the LOOP — see this module's docstring
    for why that distinction is the one the task turns on.
    """
    failure: list[BaseException] = []

    def run() -> None:
        try:
            transport(envelope)
        except BaseException as exc:  # noqa: BLE001 — the whole point
            failure.append(exc)

    thread = threading.Thread(target=run, name="autoloop-notify", daemon=True)
    thread.start()
    thread.join(envelope.timeout_seconds)
    if thread.is_alive():
        _ABANDONED.append(thread)
        log(
            f"send to {_safe(envelope.recipient, secret)} did not return within "
            f"{envelope.timeout_seconds}s and was abandoned; the round continues "
            "and nothing is retried"
        )
        return OUTCOME_TIMEOUT
    if failure:
        log(
            f"send to {_safe(envelope.recipient, secret)} failed: "
            f"{_safe(type(failure[0]).__name__, secret)}: {_safe(failure[0], secret)}"
        )
        return OUTCOME_FAILED
    return OUTCOME_SENT


# ---- the entry point --------------------------------------------------------


def notify_status_change(config, snap: StatusSnapshot, *, transport=None, log=None) -> str:
    """Email the operator IF this is a change they asked to hear about.

    Returns an outcome; raises nothing, ever. Called from `heartbeat.publish`
    inside its own `try`, so this promise is belt and braces — but it is made
    here, because every reason `publish` must not raise applies one call deeper.

    The order is load-bearing:

    1. Disabled — nothing is read, nothing is written, nothing costs anything.
    2. Unchanged against the last OBSERVED tuple — the hot-loop case.
    3. The change is RECORDED, before any send is attempted. A send that fails
       is therefore not retried on the next beat, which is what keeps a dead
       server costing one timeout per change rather than one per publish.
    4. Only then: the status filter, the credentials, and one bounded attempt.
    """
    log = log or _log_stderr
    secret = ""
    try:
        cfg = getattr(config, "notify", None)
        # `is not True`, not `not ...`: only a real boolean True sends. A
        # truthy stand-in — a string, a test double, anything a duck-typed
        # config might carry — reads as OFF, which is the direction an
        # accessory that opens a network connection has to fail in.
        if cfg is None or getattr(cfg, "enabled", False) is not True:
            return OUTCOME_DISABLED

        path = _state_path(config)
        cache_key = str(path) if path is not None else "<unresolved>"
        # The process cache WINS over the file when it is populated: it is set
        # whenever this process observes a change, so it is never older than the
        # file, and it is the only thing standing between an unwritable state
        # directory and an email per beat.
        last = _LAST_OBSERVED.get(cache_key)
        if last is None:
            last = _read_last(path)
        if last == snap.key:
            return OUTCOME_UNCHANGED

        _LAST_OBSERVED[cache_key] = snap.key
        _write_last(path, snap)

        statuses = tuple(getattr(cfg, "statuses", ()) or ())
        if snap.status not in statuses:
            return OUTCOME_FILTERED

        username, secret, refusal = _resolve_credentials(cfg)
        if refusal:
            log(
                f"NOT sending the {snap.status!r} change: {refusal}. The password "
                "is never read from the config file, and an unauthenticated send "
                "is never substituted for a missing one."
            )
            return OUTCOME_REFUSED

        outstanding = _prune_abandoned()
        if outstanding >= MAX_ABANDONED_SENDS:
            log(
                f"skipping the {snap.status!r} change: {outstanding} earlier sends "
                "are still hanging, which means the configured SMTP server is not "
                "answering"
            )
            return OUTCOME_BUSY

        # A non-positive timeout would abandon every send the instant it
        # started, and three abandoned sends switch notification off — a guard
        # turning itself off, which is the failure shape this module refuses.
        # `load_config` never produces one; a directly built config can, so it
        # falls back to the default LOUDLY rather than quietly doing nothing.
        timeout_seconds = float(getattr(cfg, "timeout_seconds", 0.0) or 0.0)
        if not timeout_seconds > 0:
            log(
                f"notify.timeout_seconds is {timeout_seconds!r}, which would "
                f"abandon every send immediately — using {DEFAULT_TIMEOUT_SECONDS}s "
                "for this one"
            )
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS

        envelope = Envelope(
            host=str(getattr(cfg, "host", "") or ""),
            port=int(getattr(cfg, "port", 0) or 0),
            tls=str(getattr(cfg, "tls", NOTIFY_TLS_STARTTLS) or NOTIFY_TLS_STARTTLS),
            timeout_seconds=timeout_seconds,
            sender=str(getattr(cfg, "sender", "") or ""),
            recipient=str(getattr(cfg, "recipient", "") or ""),
            subject=render_subject(snap, secret),
            body=render_body(snap, secret),
            username=username,
            password=secret,
        )
        outcome = _deliver(transport or smtp_transport, envelope, secret, log)
        if outcome == OUTCOME_SENT:
            log(
                f"sent {snap.status!r} (task {snap.task_id or '-'}, phase "
                f"{snap.phase or '-'}) to {_safe(envelope.recipient, secret)}"
            )
        return outcome
    except Exception as exc:  # noqa: BLE001 — a round must survive anything
        # `Exception`, not `BaseException`: a KeyboardInterrupt or a SystemExit
        # arriving in this window is the operator stopping the loop, and eating
        # one to protect a notification would be the wrong trade in the one
        # direction that matters.
        try:
            log(
                f"suppressed a failure while notifying: "
                f"{_safe(type(exc).__name__, secret)}: {_safe(exc, secret)}"
            )
        except Exception:  # noqa: BLE001 — even the log is best-effort
            pass
        return OUTCOME_ERROR
