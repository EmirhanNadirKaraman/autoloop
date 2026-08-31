"""The loop mails the operator when its status CHANGES — and never at any cost
to the round.

No SMTP and no network anywhere in this file: the transport is injected and
every test asserts against a fake. That is not only convenience — the two
behaviours that matter most (a transport that RAISES, a transport that HANGS
past the timeout) are unreachable against a real server, and the second one is
unreachable against `smtplib`'s socket timeout as well.

What is deliberately NOT pinned here: that email covers a hung, crashed or
killed loop. It cannot, from inside the loop, and nothing in this module tries
to — see `notify`'s docstring and `scripts/autoloop_health_notify.sh`.
"""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autoloop import heartbeat, notify
from autoloop.config import (
    MAX_NOTIFY_TIMEOUT_SECONDS,
    AutoloopConfig,
    BrowserConfig,
    NotifyConfig,
    PolicyConfig,
    load_config,
)
from autoloop.errors import ConfigError
from autoloop.state import LoopState

#: Distinctive enough that a substring assertion against it means something.
PASSWORD = "correct-horse-battery-staple-42"
ENV_NAME = "AUTOLOOP_TEST_SMTP_PASSWORD"
NOW = datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_process_cache():
    """`notify` remembers the last change it observed per state-file path, so a
    test that did not start clean could not tell a dedup from a leak."""
    notify.clear_process_cache()
    yield
    notify.clear_process_cache()


@pytest.fixture
def password_env(monkeypatch):
    monkeypatch.setenv(ENV_NAME, PASSWORD)
    return ENV_NAME


def make_config(tmp_path, **overrides) -> AutoloopConfig:
    settings = dict(
        enabled=True,
        recipient="operator@example.com",
        sender="loop@example.com",
        host="smtp.example.com",
        port=587,
        username="loop",
        password_env=ENV_NAME,
        timeout_seconds=0.5,
    )
    settings.update(overrides)
    return AutoloopConfig(
        browser=BrowserConfig(),
        policy=PolicyConfig(),
        state_dir=tmp_path / "repo" / ".autoloop",
        workers_root=tmp_path / "workers",
        notify=NotifyConfig(**settings),
    )


class Recorder:
    """A transport. Records what it was handed and opens nothing."""

    def __init__(self, raises: BaseException | None = None):
        self.sent: list[notify.Envelope] = []
        self._raises = raises

    def __call__(self, envelope):
        self.sent.append(envelope)
        if self._raises is not None:
            raise self._raises


class Hanger:
    """A transport that does not return until the test lets it."""

    def __init__(self):
        self.released = threading.Event()
        self.entered = threading.Event()

    def __call__(self, envelope):
        self.entered.set()
        self.released.wait(timeout=10)

    def release(self):
        self.released.set()


def snap(status="running", phase="executing", task_id="notify-01", decision="implement", **kw):
    state = LoopState(session_id="sess-1", conversation_url="")
    state.current_task = {"task_id": task_id, "decision": decision}
    return notify.snapshot(
        state, status=status, phase=phase, session_id="sess-1", now=NOW, **kw
    )


# --- the change tuple ---------------------------------------------------------


def test_a_change_sends(tmp_path, password_env):
    config = make_config(tmp_path)
    transport = Recorder()

    outcome = notify.notify_status_change(config, snap(), transport=transport)

    assert outcome == notify.OUTCOME_SENT
    assert len(transport.sent) == 1
    assert transport.sent[0].recipient == "operator@example.com"


def test_an_unchanged_tuple_sends_nothing(tmp_path, password_env):
    """`publish` is called from the hot loop. Emailing on every WRITE is the
    flood this whole module is shaped to avoid."""
    config = make_config(tmp_path)
    transport = Recorder()

    notify.notify_status_change(config, snap(), transport=transport)
    # A LATER beat: same tuple, different timestamp, different detail and a
    # different blocker count — none of which is part of the change tuple.
    again = notify.notify_status_change(
        config,
        snap(detail="a later beat", open_blockers=3),
        transport=transport,
    )

    assert again == notify.OUTCOME_UNCHANGED
    assert len(transport.sent) == 1


@pytest.mark.parametrize(
    "field,value",
    [("status", "parked"), ("phase", "awaiting"), ("task_id", "other-02"),
     ("decision", "revise")],
)
def test_every_element_of_the_tuple_is_a_change(tmp_path, password_env, field, value):
    config = make_config(tmp_path)
    transport = Recorder()
    notify.notify_status_change(config, snap(), transport=transport)

    notify.notify_status_change(config, snap(**{field: value}), transport=transport)

    assert len(transport.sent) == 2, f"a change of {field} must notify"


def test_a_restart_does_not_resend_the_state_it_already_sent(tmp_path, password_env):
    """The dedup is persisted beside the heartbeat, so it survives the process.
    `clear_process_cache` IS the restart: it drops everything a process holds in
    memory and leaves only the file."""
    config = make_config(tmp_path)
    transport = Recorder()
    notify.notify_status_change(config, snap(), transport=transport)
    assert config.notify_state_file.exists()

    notify.clear_process_cache()
    outcome = notify.notify_status_change(config, snap(), transport=transport)

    assert outcome == notify.OUTCOME_UNCHANGED
    assert len(transport.sent) == 1


def test_the_persisted_record_lives_beside_the_heartbeat(tmp_path, password_env):
    config = make_config(tmp_path)
    notify.notify_status_change(config, snap(), transport=Recorder())

    assert config.notify_state_file.parent == config.heartbeat_file.parent
    # And therefore outside the checkout, which is what earns it a place in
    # `test_state_dir_location.EXTERNAL_BY_DESIGN`: it is written mid-round,
    # and the escape detector snapshots the checkout including ignored paths.
    assert config.state_dir.parent not in config.notify_state_file.parents
    record = json.loads(config.notify_state_file.read_text(encoding="utf-8"))
    assert record["status"] == "running"
    assert record["task_id"] == "notify-01"
    assert record["decision"] == "implement"


def test_a_corrupt_record_sends_rather_than_going_quiet(tmp_path, password_env):
    """The direction matters: an unreadable record means the loop cannot prove
    it already reported this state, and one duplicate mail is a far better
    failure than a park nobody hears about."""
    config = make_config(tmp_path)
    config.notify_state_file.parent.mkdir(parents=True, exist_ok=True)
    config.notify_state_file.write_text("{not json", encoding="utf-8")
    transport = Recorder()

    assert notify.notify_status_change(config, snap(), transport=transport) == (
        notify.OUTCOME_SENT
    )
    assert len(transport.sent) == 1


def test_an_unwritable_record_still_cannot_flood(tmp_path, password_env, monkeypatch):
    """The failure the in-process cache exists for: with the state file
    unwritable, "email on a change" would otherwise become "email on every
    beat" — and `publish` runs in the hot loop."""
    config = make_config(tmp_path)
    monkeypatch.setattr(notify, "_write_last", lambda path, snapshot: False)
    transport = Recorder()

    for _ in range(5):
        notify.notify_status_change(config, snap(), transport=transport)

    assert len(transport.sent) == 1


# --- what does and does not get mailed ----------------------------------------


def test_disabled_reads_nothing_writes_nothing_sends_nothing(tmp_path, password_env):
    """An unconfigured loop must not try to send."""
    config = make_config(tmp_path, enabled=False)
    transport = Recorder()

    outcome = notify.notify_status_change(config, snap(), transport=transport)

    assert outcome == notify.OUTCOME_DISABLED
    assert transport.sent == []
    assert not config.notify_state_file.exists()


def test_only_a_real_boolean_true_sends(tmp_path, password_env):
    """A truthy stand-in reads as OFF. An accessory that opens a network
    connection has to fail in that direction."""
    config = make_config(tmp_path, enabled=False)
    object.__setattr__(config.notify, "enabled", "yes")
    transport = Recorder()

    assert notify.notify_status_change(config, snap(), transport=transport) == (
        notify.OUTCOME_DISABLED
    )
    assert transport.sent == []


def test_a_status_outside_the_list_is_recorded_but_not_mailed(tmp_path, password_env):
    config = make_config(tmp_path, statuses=("parked", "blocked"))
    transport = Recorder()

    outcome = notify.notify_status_change(config, snap(status="running"), transport=transport)

    assert outcome == notify.OUTCOME_FILTERED
    assert transport.sent == []
    assert config.notify_state_file.exists(), "a filtered change is still observed"


def test_a_park_after_a_run_notifies_again(tmp_path, password_env):
    """The reason the persisted tuple is the last OBSERVED one rather than the
    last MAILED one. With statuses narrowed to parks, park -> running -> park is
    two alarms; a last-mailed tuple would suppress the second, which is the one
    failure this feature cannot have."""
    config = make_config(tmp_path, statuses=("parked",))
    transport = Recorder()

    notify.notify_status_change(config, snap(status="parked"), transport=transport)
    notify.notify_status_change(config, snap(status="running"), transport=transport)
    notify.notify_status_change(config, snap(status="parked"), transport=transport)

    assert len(transport.sent) == 2


# --- containment: it must never break a round ---------------------------------


def test_a_raising_transport_does_not_propagate(tmp_path, password_env):
    config = make_config(tmp_path)
    transport = Recorder(raises=RuntimeError("connection refused"))

    outcome = notify.notify_status_change(config, snap(), transport=transport)

    assert outcome == notify.OUTCOME_FAILED


def test_a_raising_transport_leaves_the_round_running(tmp_path, password_env, capsys):
    """Driven through the REAL `heartbeat.publish`, which is the call the loop
    makes: the beat is still written and nothing is raised."""
    config = make_config(tmp_path)
    state = LoopState(session_id="sess-1", conversation_url="", phase="executing")

    heartbeat.publish(
        config,
        state,
        notify_transport=Recorder(raises=OSError("smtp is dead")),
    )

    beat = json.loads(config.heartbeat_file.read_text(encoding="utf-8"))
    assert beat["status"] == "running" and beat["phase"] == "executing"
    assert "failed" in capsys.readouterr().err


def test_a_hanging_transport_is_cut_off_at_the_timeout(tmp_path, password_env):
    config = make_config(tmp_path, timeout_seconds=0.05)
    hanger = Hanger()
    try:
        started = time.monotonic()
        outcome = notify.notify_status_change(config, snap(), transport=hanger)
        elapsed = time.monotonic() - started
    finally:
        hanger.release()

    assert outcome == notify.OUTCOME_TIMEOUT
    # Waited for rather than sampled: the send is on its own thread, and under a
    # loaded machine it may not have been scheduled by the time the join expired.
    assert hanger.entered.wait(timeout=5), "the send really was attempted"
    assert elapsed < 3.0, f"the round waited {elapsed:.2f}s on a hung server"


def test_a_hanging_transport_leaves_the_round_running(tmp_path, password_env):
    config = make_config(tmp_path, timeout_seconds=0.05)
    state = LoopState(session_id="sess-1", conversation_url="", phase="executing")
    hanger = Hanger()
    try:
        started = time.monotonic()
        heartbeat.publish(config, state, notify_transport=hanger)
        elapsed = time.monotonic() - started
    finally:
        hanger.release()

    assert config.heartbeat_file.exists()
    assert elapsed < 3.0


def test_a_dead_server_is_not_retried_on_the_next_beat(tmp_path, password_env):
    """The change is recorded BEFORE the attempt, so a dead server costs one
    timeout per status CHANGE and nothing per publish. Recording on success
    instead would make every later beat in the hot loop pay it again."""
    config = make_config(tmp_path)
    transport = Recorder(raises=RuntimeError("no route to host"))

    for _ in range(4):
        notify.notify_status_change(config, snap(), transport=transport)

    assert len(transport.sent) == 1


def test_hung_sends_cannot_pile_up_without_bound(tmp_path, password_env):
    """Each abandoned send holds a socket. A server that accepts and never
    answers would otherwise leak one per status change for the life of the run."""
    config = make_config(tmp_path, timeout_seconds=0.02)
    hanger = Hanger()
    outcomes = []
    try:
        for n in range(notify.MAX_ABANDONED_SENDS + 2):
            outcomes.append(
                notify.notify_status_change(
                    config, snap(task_id=f"task-{n}"), transport=hanger
                )
            )
    finally:
        hanger.release()

    assert outcomes.count(notify.OUTCOME_TIMEOUT) == notify.MAX_ABANDONED_SENDS
    assert outcomes[-1] == notify.OUTCOME_BUSY


def test_publish_survives_a_notify_module_that_blows_up(tmp_path, monkeypatch):
    """The belt to the module's own braces: even if nothing inside `notify`
    worked, the heartbeat is still published."""
    config = make_config(tmp_path)

    def explode(*args, **kwargs):
        raise MemoryError("boom")

    monkeypatch.setattr(notify, "notify_status_change", explode)
    heartbeat.publish(config, LoopState(session_id="s", conversation_url=""))

    assert config.heartbeat_file.exists()


def test_a_broken_state_object_notifies_rather_than_raising(tmp_path, password_env):
    """As tolerant as `publish` is: `current_task` is whatever a hand-edited
    state file contains."""
    config = make_config(tmp_path)
    state = LoopState(session_id="s", conversation_url="")
    state.current_task = "not a dict"
    transport = Recorder()

    built = notify.snapshot(state, status="parked", phase="ready", now=NOW)
    assert built.task_id == "" and built.decision == ""
    assert notify.notify_status_change(config, built, transport=transport) == (
        notify.OUTCOME_SENT
    )


# --- the secret ---------------------------------------------------------------


def test_the_password_comes_from_the_environment(tmp_path, password_env):
    config = make_config(tmp_path)
    transport = Recorder()

    notify.notify_status_change(config, snap(), transport=transport)

    assert transport.sent[0].password == PASSWORD
    assert transport.sent[0].username == "loop"


def test_the_password_can_come_from_a_file(tmp_path):
    secret_file = tmp_path / "smtp-password"
    secret_file.write_text(PASSWORD + "\n", encoding="utf-8")
    config = make_config(tmp_path, password_env="", password_file=str(secret_file))
    transport = Recorder()

    notify.notify_status_change(config, snap(), transport=transport)

    assert transport.sent[0].password == PASSWORD


def test_a_password_file_yields_one_line_and_not_the_whole_file(tmp_path):
    """A trailing newline is what an editor leaves behind; a second line must
    not be carried into the credential."""
    secret_file = tmp_path / "smtp-password"
    secret_file.write_text(f"\n{PASSWORD}\nnotes about this account\n", encoding="utf-8")
    config = make_config(tmp_path, password_env="", password_file=str(secret_file))
    transport = Recorder()

    notify.notify_status_change(config, snap(), transport=transport)

    assert transport.sent[0].password == PASSWORD


@pytest.mark.parametrize(
    "overrides,setenv,fragment",
    [
        ({}, None, "unset or empty"),
        ({}, "", "unset or empty"),
        ({"password_env": "", "password_file": "MISSING"}, None, "missing, unreadable"),
        ({"password_env": "", "password_file": "BLANK"}, None, "missing, unreadable"),
        ({"password_env": "", "password_file": ""}, PASSWORD, "neither notify.password_env"),
        ({"username": ""}, PASSWORD, "no notify.username is configured"),
        (
            {"password_file": "BLANK"},
            PASSWORD,
            "both set",
        ),
    ],
)
def test_no_usable_password_refuses_rather_than_sending_unauthenticated(
    tmp_path, monkeypatch, capsys, overrides, setenv, fragment
):
    """Every "absent" fails the same way, because they mean the same thing: an
    unset variable, one set to the empty string, a missing file, a blank file,
    no source at all, and no username. None of them may fall through to an
    unauthenticated send — that is the guard which would otherwise switch itself
    off exactly when the credential is broken."""
    if setenv is None:
        monkeypatch.delenv(ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(ENV_NAME, setenv)
    if overrides.get("password_file") == "MISSING":
        overrides["password_file"] = str(tmp_path / "nope" / "secret")
    if overrides.get("password_file") == "BLANK":
        blank = tmp_path / "blank"
        blank.write_text("   \n", encoding="utf-8")
        overrides["password_file"] = str(blank)
    config = make_config(tmp_path, **overrides)
    transport = Recorder()

    outcome = notify.notify_status_change(config, snap(), transport=transport)

    assert outcome == notify.OUTCOME_REFUSED
    assert transport.sent == []
    assert fragment in capsys.readouterr().err


def test_the_password_appears_in_nothing_that_is_rendered_or_logged(
    tmp_path, password_env, capsys
):
    """The claim, stated as one test. The transport is handed the password —
    that is its job — and everything else must be free of it, including an
    exception string nobody anticipated, which is why `_safe` is a choke point
    rather than a habit."""
    config = make_config(tmp_path)
    # An SMTP client that echoes the credential back inside its error is exactly
    # the case a careful-by-hand redaction would miss.
    transport = Recorder(
        raises=RuntimeError(f"535 auth failed for loop:{PASSWORD} at smtp")
    )

    notify.notify_status_change(
        config, snap(detail=f"a detail mentioning {PASSWORD}"), transport=transport
    )

    envelope = transport.sent[0]
    assert PASSWORD not in envelope.subject
    assert PASSWORD not in envelope.body
    assert notify.REDACTED in envelope.body
    assert PASSWORD not in repr(envelope), "the dataclass repr must not carry it"
    captured = capsys.readouterr()
    assert PASSWORD not in captured.err and PASSWORD not in captured.out
    assert notify.REDACTED in captured.err
    assert PASSWORD not in config.notify_state_file.read_text(encoding="utf-8")


# --- what the mail says -------------------------------------------------------


def test_the_subject_carries_the_identity_at_a_glance(tmp_path, password_env):
    config = make_config(tmp_path)
    transport = Recorder()

    notify.notify_status_change(
        config, snap(status="blocked", phase="awaiting"), transport=transport
    )

    subject = transport.sent[0].subject
    assert "blocked" in subject and "notify-01" in subject and "awaiting" in subject


def test_the_body_carries_what_status_would_have_told_you(tmp_path, password_env):
    config = make_config(tmp_path)
    transport = Recorder()

    notify.notify_status_change(
        config,
        snap(status="parked", phase="ready", detail="the reviewer asked a question",
             open_blockers=2),
        transport=transport,
    )

    body = transport.sent[0].body
    for fragment in (
        "status:        parked", "phase:         ready", "task:          notify-01",
        "decision:      implement", "open blockers: 2", "session:       sess-1",
        NOW.isoformat(timespec="seconds"), "the reviewer asked a question",
    ):
        assert fragment in body, f"the body does not carry {fragment!r}"


def test_the_body_says_what_email_cannot_cover(tmp_path, password_env):
    """The limitation, in the mail itself: the person reading this at 3am is
    exactly the one who must not read silence as health."""
    config = make_config(tmp_path)
    transport = Recorder()

    notify.notify_status_change(config, snap(), transport=transport)

    body = transport.sent[0].body.lower()
    assert "hung" in body and "crashed" in body
    assert "not arriving means nothing" in body


def test_no_absolute_path_out_of_the_operators_home_is_mailed(tmp_path, password_env):
    home = str(Path.home())
    if home in ("", "/"):
        pytest.skip("no distinguishable home directory on this host")
    config = make_config(tmp_path)
    transport = Recorder()

    notify.notify_status_change(
        config,
        snap(detail=f"worker repo {home}/.autoloop/workers/notify-01 is dirty"),
        transport=transport,
    )

    body = transport.sent[0].body
    assert home not in body
    assert "~/.autoloop/workers/notify-01" in body


def test_a_newline_in_the_state_cannot_forge_a_header(tmp_path, password_env):
    """`task_id` and `phase` come from a state file and land in the Subject."""
    config = make_config(tmp_path)
    transport = Recorder()

    notify.notify_status_change(
        config, snap(task_id="t-1\nBcc: attacker@example.com"), transport=transport
    )

    assert "\n" not in transport.sent[0].subject
    assert "\r" not in transport.sent[0].subject


def test_the_subject_is_bounded_however_long_the_state_is(tmp_path, password_env):
    """Every part of the Subject is read out of a state file."""
    config = make_config(tmp_path)
    transport = Recorder()

    notify.notify_status_change(config, snap(task_id="t" * 5000), transport=transport)

    assert len(transport.sent[0].subject) == notify.MAX_SUBJECT_CHARS


def test_an_unusable_timeout_falls_back_loudly(tmp_path, password_env, capsys):
    """`load_config` refuses a non-positive timeout, so only a config built in
    code reaches this — and reaching it must not mean "abandon every send
    instantly", which would switch notification off after three changes."""
    config = make_config(tmp_path, timeout_seconds=0)
    transport = Recorder()

    outcome = notify.notify_status_change(config, snap(), transport=transport)

    assert outcome == notify.OUTCOME_SENT
    assert "abandon every send immediately" in capsys.readouterr().err


def test_the_detail_is_bounded_exactly_as_the_beat_is(tmp_path, password_env):
    """The mail and the heartbeat must not disagree about what the loop said."""
    built = notify.snapshot(None, status="parked", detail="x" * 5000, now=NOW)
    assert len(built.detail) == notify.MAX_DETAIL_CHARS == 300


# --- the hook -----------------------------------------------------------------


def test_publish_is_the_chokepoint(tmp_path, password_env):
    """Hooked behind `publish` so a new call site cannot forget to notify."""
    config = make_config(tmp_path)
    state = LoopState(session_id="sess-1", conversation_url="", phase="executing")
    transport = Recorder()

    heartbeat.publish(config, state, notify_transport=transport)
    heartbeat.publish(config, state, notify_transport=transport)  # unchanged
    heartbeat.publish(config, state, heartbeat.PAUSED, notify_transport=transport)

    assert [envelope.subject.split("|")[0].strip() for envelope in transport.sent] == [
        "[autoloop] running", "[autoloop] paused",
    ]


def test_publish_notifies_the_status_it_actually_published(tmp_path, password_env):
    """`publish` downgrades RUNNING to BLOCKED when a blocker is open, and the
    mail has to say what the beat says — so the hook sits AFTER that
    resolution, not before it."""
    from autoloop.blockers import BlockerStore

    config = make_config(tmp_path)
    BlockerStore(config.blockers_dir).record(
        task_id="t-1", kind="task_fatal", code="approved_paths_missing",
        question="task t-1 has no approved_paths", detail="",
        phase="executing", now=NOW.isoformat(timespec="seconds"),
    )
    transport = Recorder()

    heartbeat.publish(
        config, LoopState(session_id="s", conversation_url=""), notify_transport=transport
    )

    assert "blocked" in transport.sent[0].subject
    assert "open blockers: 1" in transport.sent[0].body


def test_publish_sends_nothing_for_a_config_with_no_notify_section(tmp_path):
    """Every `AutoloopConfig(...)` built directly across this suite predates the
    field and must stay silent."""
    config = AutoloopConfig(
        browser=BrowserConfig(),
        policy=PolicyConfig(),
        state_dir=tmp_path / "repo" / ".autoloop",
        workers_root=tmp_path / "workers",
    )
    transport = Recorder()

    heartbeat.publish(config, LoopState(session_id="s", conversation_url=""),
                      notify_transport=transport)

    assert transport.sent == []
    assert config.heartbeat_file.exists()


# --- configuration ------------------------------------------------------------


def write_config(tmp_path, body: str) -> Path:
    """`body` FIRST, `[paths]` after it: a case that writes a bare `notify = ...`
    key is testing a top-level value, and appending it under a section header
    would silently make it a key of that section instead."""
    path = tmp_path / "config.toml"
    workers = str(tmp_path / "workers")
    path.write_text(
        body + '\n[paths]\nworkers_root = "' + workers + '"\n', encoding="utf-8"
    )
    return path


def test_a_config_with_no_notify_section_loads_switched_off(tmp_path):
    config = load_config(write_config(tmp_path, ""))

    assert config.notify.enabled is False
    assert config.notify == NotifyConfig()


def test_a_disabled_section_need_not_be_filled_in(tmp_path):
    config = load_config(write_config(tmp_path, "[notify]\nenabled = false\n"))

    assert config.notify.enabled is False


def test_a_complete_section_loads(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            '[notify]\nenabled = true\nrecipient = "op@example.com"\n'
            'sender = "loop@example.com"\nhost = "smtp.example.com"\nport = 465\n'
            'tls = "ssl"\nusername = "loop"\npassword_env = "SMTP_PW"\n'
            'timeout_seconds = 5.0\nstatuses = ["parked", "blocked"]\n',
        )
    )

    assert config.notify.enabled is True
    assert config.notify.tls == "ssl" and config.notify.port == 465
    assert config.notify.statuses == ("parked", "blocked")


@pytest.mark.parametrize(
    "body,fragment",
    [
        ('[notify]\npassword = "hunter2"\n', "never be one"),
        ('[notify]\nsmtp_password = "hunter2"\n', "never be one"),
        ('[notify]\nnope = "x"\n', "unknown keys in [notify]"),
        ('[notify]\nenabled = "true"\n', "must be a boolean"),
        ("[notify]\nport = 0\n", "between 1 and 65535"),
        ("[notify]\nport = true\n", "between 1 and 65535"),
        ('[notify]\ntls = "none"\n', "put your password on the wire"),
        ('[notify]\ntls = "tls"\n', "notify.tls must be one of"),
        ("[notify]\ntimeout_seconds = 0\n", "positive number"),
        ("[notify]\ntimeout_seconds = 600.0\n", "at most"),
        ("[notify]\nstatuses = []\n", "read as configured while sending nothing"),
        ('[notify]\nstatuses = ["running", "wedged"]\n', "never publishes"),
        ('[notify]\nrecipient = "a@b\\nBcc: c@d"\n', "no line breaks"),
        ('[notify]\nhost = 25\n', "must be a string"),
        ('notify = "on"\n', "[notify] must be a table"),
    ],
)
def test_a_malformed_section_is_refused_with_the_reason(tmp_path, body, fragment):
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, body))
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    "missing,fragment",
    [
        ("recipient", "notify.recipient is required"),
        ("sender", "notify.sender is required"),
        ("host", "notify.host is required"),
        ("username", "notify.username is required"),
    ],
)
def test_enabling_it_requires_everything_a_send_needs(tmp_path, missing, fragment):
    keys = {
        "recipient": '"op@example.com"',
        "sender": '"loop@example.com"',
        "host": '"smtp.example.com"',
        "username": '"loop"',
    }
    keys.pop(missing)
    body = "[notify]\nenabled = true\npassword_env = \"SMTP_PW\"\n" + "".join(
        f"{key} = {value}\n" for key, value in keys.items()
    )

    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, body))
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    "sources",
    ["", 'password_env = "SMTP_PW"\npassword_file = "/tmp/pw"\n'],
)
def test_exactly_one_password_source_is_required_when_enabled(tmp_path, sources):
    body = (
        '[notify]\nenabled = true\nrecipient = "op@example.com"\n'
        'sender = "loop@example.com"\nhost = "smtp.example.com"\n'
        'username = "loop"\n' + sources
    )

    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, body))
    assert "exactly one of notify.password_env" in str(exc.value)


def test_the_password_never_appears_in_a_config_error(tmp_path):
    """The refusal names the key, never its value — an error message is exactly
    the kind of string that ends up in a log."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, f'[notify]\npassword = "{PASSWORD}"\n'))
    assert PASSWORD not in str(exc.value)


def test_the_template_ships_the_section_off_and_documents_every_key():
    """The template is copied once and never re-read, so one that shipped this
    on would try to send mail from every new deployment."""
    import dataclasses

    example = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text(
        encoding="utf-8"
    )
    section = example.split("[notify]", 1)[1].split("\n[", 1)[0]
    assert "enabled = false" in section
    for field in dataclasses.fields(NotifyConfig):
        assert f"{field.name} =" in section, f"the template does not document {field.name}"
    # The limitation, in the file an operator actually reads.
    assert "autoloop_health_notify.sh" in section
    assert "Mail NOT arriving means nothing at all." in section
    assert str(int(MAX_NOTIFY_TIMEOUT_SECONDS)) in section
    assert PASSWORD not in example
