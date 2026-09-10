"""The agent action log: streamed while the round is still running, bounded,
default off, and incapable of failing a round.

The claim this file grades has two halves and both are pinned here:

* WITH THE FLAG OFF the round behaves byte for byte as it did before the log
  existed — no directory, no file, no stderr line, and an `AgentResult` equal
  field for field to the one the same run produces with the log absent.
* WITH THE FLAG ON a round through the executor path produces a NON-EMPTY log
  WHILE THE PROCESS IS STILL ALIVE. `test_output_reaches_the_log_while_the_
  process_is_still_running` and its end-to-end sibling are written so that the
  mutation "copy the temp files once, after `supervise` returns" FAILS them:
  both read the file from inside the supervisor's own sleep, at a moment when
  the fake process has not exited and will not for several more ticks. A
  write-at-exit implementation leaves an identical file behind and still fails
  here, which is the entire point of the task.

Section 7 grades the WIRE between those two halves, which is the part
stream-01 did not have: a `[audit] action_log = true` written in a real config
file, read by the real `load_config`, handed by the real `cli._build_executor`
to the real `implement_agent_runner`, and arriving at the write-capable runner
a round really runs. Nothing there constructs a runner by hand or asserts on
`cli.py`'s source text: a test that DESCRIBES production rather than using it
can pass while production stays inert, which is the failure this whole file
exists to make impossible.

The wire is an ARGUMENT and nothing in the process is armed, so section 7 also
pins where the argument does and does not go: the write-capable runner gets the
configured directory with the flag on and `None` with it off, and the read-only
audit runners `cli._build_executor` builds alongside it get `None` either way.
That last one is not tidiness — an audit runner is
`subprocess.run(capture_output=True)`, so a file opened for it could only ever
have been written after the process exited, which is the one thing this setting
promises it is not.

No real `claude` process is ever spawned and nothing ever really waits: the
supervised path is driven by a fake spawn, a fake handle and a fake clock, the
same way `test_stall_detector.py` drives it. Section 7 uses real git
repositories, because the claims it makes are about the real executor path.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

import pytest

from gitrepo import make_repo_from_template

from autoloop import cli
from autoloop.audit.agents import (
    ACTION_LOG_BUFFERED_NOTICE,
    DEFAULT_ACTION_LOG_MAX_BYTES,
    AgentSpec,
    ClaudeCliRunner,
    action_log_round_stamp,
    action_log_slug,
    open_action_log,
)
from autoloop.config import AuditConfig, AutoloopConfig, BrowserConfig, load_config
from autoloop.contract import Decision, Directive
from autoloop.errors import ConfigError
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import (
    IMPLEMENT_DISALLOWED_TOOLS,
    WRITE_ALLOWED_TOOLS,
    ImplementExecutor,
    implement_agent_runner,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.stall import PartialWork, ProgressSample, StallPolicy
from autoloop.tasks import Task, TaskRegistry
from autoloop.worker_env import WorkerRepoManager

SPEC = AgentSpec(domain="t1", title="Add widget", prompt="do the thing")

#: Every phrase this file refuses to find anywhere the log is surfaced. The
#: brief is explicit: it is an ACTION log, and labelling process output as the
#: model's thinking misrepresents what the operator is reading.
THINKING_WORDS = ("thinking", "reasoning", "chain of thought", "thought process")


# NO FIXTURE RESTORES ANYTHING HERE, and that is a property worth naming rather
# than an omission: the action log is configured by an ARGUMENT, so nothing in
# this file can arm a runner it did not construct. An earlier design armed a
# process-wide default from `load_config` and needed an autouse fixture to stop
# a `[audit] action_log = true` written in one test from switching logging on
# inside unrelated tests elsewhere in the suite. There is nothing left to leak.


# ---- fakes ------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ChattyHandle:
    """A process shaped like the one this loop really runs.

    `--output-format json` makes the CLI print ONE json object on stdout when
    it exits, while its progress goes to stderr throughout — so that is what
    this fake does. Modelling it the other way round would let a test claim
    the stdout half streams, which it does not, and the log's own header says
    as much.

    It appends with an explicit `seek(0, 2)` because a fake shares one file
    object with the pump, whereas a real child holds its own descriptor. That
    is the pessimistic model on purpose: the pump must not move any offset,
    and this handle notices if it does.
    """

    def __init__(
        self,
        clock,
        stdout,
        stderr,
        exit_at,
        per_poll=b"",
        result=b'{"result": "done"}',
    ):
        self._clock = clock
        self._stdout = stdout
        self._stderr = stderr
        self._exit_at = exit_at
        self._per_poll = per_poll
        self._result = result
        self._rc = None
        self.polls = 0
        self.terminated = 0
        self.killed = 0

    @property
    def alive(self) -> bool:
        return self._rc is None

    def poll(self):
        if self._rc is None and self._clock() >= self._exit_at:
            self._append(self._stdout, self._result)
            self._rc = 0
            return self._rc
        if self._rc is None:
            self.polls += 1
            self._append(self._stderr, self._per_poll)
        return self._rc

    def _append(self, fileobj, payload):
        if not payload:
            return
        fileobj.seek(0, 2)
        fileobj.write(payload)
        fileobj.flush()

    def terminate(self):
        self.terminated += 1
        self._rc = -15

    def kill(self):
        self.killed += 1
        self._rc = -9


class WritingProbe:
    """An agent that keeps changing the tree — every sample differs, so the
    stall detector never fires and the run is bounded only by its exit."""

    def __init__(self):
        self.samples = 0

    def sample(self):
        self.samples += 1
        return ProgressSample(files=1, marks=(("feature.py", self.samples, self.samples),))

    def partial_work(self):
        return PartialWork(files_changed=1, lines_written=self.samples)


class UnusedRunner:
    """`ImplementExecutor`'s standalone `agent_runner`, which every test here
    supplies a factory instead of. Calling it is a bug in the test."""

    def run(self, spec):  # pragma: no cover - a failure, not a path
        raise AssertionError("the standalone agent_runner must never be reached")


def make_policy(stall=1800.0, ceiling=14400.0, poll=10.0):
    return StallPolicy(stall_seconds=stall, ceiling_seconds=ceiling, poll_seconds=poll)


def supervised_runner(tmp_path, spawn, clock, **kwargs):
    return ClaudeCliRunner(
        tmp_path,
        progress_probe=WritingProbe(),
        stall_policy=make_policy(),
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )


def chatty_spawn(clock, exit_at=50.0, per_poll=b"tool: Read(README.md)\n"):
    """A spawn that hands back a `ChattyHandle`, and remembers it."""
    box = {}

    def spawn(argv, *, cwd, env, stdout, stderr):
        handle = ChattyHandle(clock, stdout, stderr, exit_at, per_poll=per_poll)
        box["handle"] = handle
        return handle

    return spawn, box


def only_log(directory: Path) -> Path:
    logs = sorted(Path(directory).glob("*.log"))
    assert len(logs) == 1, f"expected exactly one action log, found {logs}"
    return logs[0]


# ---- 1. the flag defaults off, and off is the behaviour that was here before --


def test_the_setting_defaults_off():
    assert AuditConfig().action_log is False


def test_a_config_with_no_action_log_key_loads_it_off(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[paths]\nworkers_root = "/tmp/autoloop-workers-defaults-off"\n[audit]\n'
    )
    assert load_config(config).audit.action_log is False


def test_a_non_boolean_action_log_is_refused_not_read_as_truthy(tmp_path):
    """`action_log = "false"` is a non-empty string and therefore truthy. A
    default-off flag that switches itself ON for a typo is the one direction
    this must never fail in."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[paths]\nworkers_root = "/tmp/autoloop-workers-typo"\n'
        '[audit]\naction_log = "false"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "audit.action_log must be a boolean" in str(exc.value)


def test_the_log_directory_is_under_the_state_dir(tmp_path):
    config = AutoloopConfig(
        browser=BrowserConfig(), policy=PolicyConfig(), state_dir=tmp_path / "state"
    )
    assert config.action_log_dir == tmp_path / "state" / "action-logs"


def test_with_the_log_off_no_file_and_no_directory_appear(tmp_path, capsys):
    """Off means OFF: not an empty file, not an empty directory, not a line on
    stderr. Anything created here would be a behaviour change shipped under a
    flag that is supposed to change nothing."""
    clock = FakeClock()
    spawn, _ = chatty_spawn(clock)
    log_dir = tmp_path / "action-logs"

    result = supervised_runner(tmp_path, spawn, clock).run(SPEC)

    assert result.ok
    assert not log_dir.exists()
    assert capsys.readouterr().err == ""


def test_the_captured_result_is_identical_with_and_without_the_log(tmp_path):
    """Outcome parsing depends on the captured text, so streaming has to be an
    ADDITION. Same fake process, twice; every field of the result must match."""
    clock_off = FakeClock()
    spawn_off, _ = chatty_spawn(clock_off)
    without = supervised_runner(tmp_path, spawn_off, clock_off).run(SPEC)

    clock_on = FakeClock()
    spawn_on, _ = chatty_spawn(clock_on)
    with_log = supervised_runner(
        tmp_path, spawn_on, clock_on, action_log_dir=tmp_path / "logs"
    ).run(SPEC)

    assert with_log.raw_text == without.raw_text == "done"
    assert with_log.returncode == without.returncode
    assert with_log.error == without.error
    assert with_log.stall is without.stall is None
    assert with_log.command == without.command


def test_the_unsupervised_path_still_captures_output_the_same_way(tmp_path):
    """The audit path is `subprocess.run(capture_output=True)` and stays that
    way with the log on — the kwargs are asserted, not assumed, because
    swapping it for pipes is how the returned bytes would stop matching."""
    seen = []

    def stub(argv, **kwargs):
        seen.append(kwargs)

        class Proc:
            returncode = 0
            stdout = json.dumps({"result": "audited"})
            stderr = ""

        return Proc()

    result = ClaudeCliRunner(
        tmp_path, runner=stub, action_log_dir=tmp_path / "logs"
    ).run(SPEC)

    assert result.raw_text == "audited"
    assert seen[0]["capture_output"] is True
    assert seen[0]["text"] is True
    assert seen[0]["timeout"] == 900.0


# ---- 2. THE claim: the log is non-empty while the process is still running ----


def test_output_reaches_the_log_while_the_process_is_still_running(tmp_path):
    """THE mutation guard for this whole change.

    The log is read from inside the supervisor's own sleep, so every sample is
    taken at a moment when the fake process has NOT exited. An implementation
    that copies the captured output once, after `supervise` returns, leaves a
    file that is identical at the end and empty at every one of these
    moments — and fails here.
    """
    clock = FakeClock()
    spawn, box = chatty_spawn(clock, exit_at=50.0, per_poll=b"tool: Read(README.md)\n")
    log_dir = tmp_path / "logs"
    seen = []

    def watching_sleep(seconds):
        log = next(iter(log_dir.glob("*.log")), None)
        seen.append(
            (
                box["handle"].alive,
                log.read_bytes() if log is not None else b"",
            )
        )
        clock.sleep(seconds)

    runner = ClaudeCliRunner(
        tmp_path,
        progress_probe=WritingProbe(),
        stall_policy=make_policy(),
        spawn=spawn,
        clock=clock,
        sleep=watching_sleep,
        action_log_dir=log_dir,
    )

    result = runner.run(SPEC)

    assert result.ok
    live_and_recorded = [
        body for alive, body in seen if alive and b"tool: Read(README.md)" in body
    ]
    assert live_and_recorded, (
        "the action log held none of the agent's output at any moment while the "
        f"process was still alive; samples were {[(a, len(b)) for a, b in seen]}"
    )
    # And the finished file still holds everything, so streaming did not cost
    # the completeness the buffered behaviour had.
    assert box["handle"].polls >= 2
    body = only_log(log_dir).read_text()
    assert body.count("tool: Read(README.md)") == box["handle"].polls


def test_both_streams_reach_the_log_and_each_block_names_its_stream(tmp_path):
    """stderr while the run is live, the json result on stdout at the end —
    and the file says which is which, because a reader who cannot tell the
    two apart is reading one interleaved blob."""
    clock = FakeClock()
    spawn, _ = chatty_spawn(clock, exit_at=30.0, per_poll=b"warn\n")
    log_dir = tmp_path / "logs"

    supervised_runner(tmp_path, spawn, clock, action_log_dir=log_dir).run(SPEC)

    body = only_log(log_dir).read_text()
    assert "[stdout]" in body
    assert "[stderr]" in body
    assert "warn" in body
    assert '"result": "done"' in body


def test_a_killed_run_keeps_what_it_had_already_produced(tmp_path):
    """A stalled agent is exactly when the log is worth most — it is the only
    account of what the agent was doing when it wedged."""

    class SilentProbe:
        def sample(self):
            return ProgressSample(files=1, marks=(("feature.py", 1, 1),))

        def partial_work(self):
            return PartialWork(files_changed=1, lines_written=1)

    clock = FakeClock()
    spawn, _ = chatty_spawn(clock, exit_at=10_000.0, per_poll=b"still working\n")
    log_dir = tmp_path / "logs"

    result = ClaudeCliRunner(
        tmp_path,
        progress_probe=SilentProbe(),
        stall_policy=make_policy(stall=60.0, ceiling=600.0, poll=10.0),
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        action_log_dir=log_dir,
    ).run(SPEC)

    assert result.stall is not None
    assert "still working" in only_log(log_dir).read_text()


# ---- 3. the log can never fail a round ---------------------------------------


def test_a_log_that_cannot_be_opened_leaves_the_round_unaffected(tmp_path, capsys):
    """The directory's parent is an ordinary FILE, so `mkdir` cannot succeed —
    a real failure, not a patched one."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    clock = FakeClock()
    spawn, _ = chatty_spawn(clock)

    result = supervised_runner(
        tmp_path, spawn, clock, action_log_dir=blocker / "logs"
    ).run(SPEC)

    assert result.ok
    assert result.raw_text == "done"
    assert result.error == ""
    # Announced, never swallowed: a log that silently stopped recording is
    # worth less than no log, because nothing says the alarm went quiet.
    assert "action log" in capsys.readouterr().err


def test_a_write_that_fails_mid_run_leaves_the_round_unaffected(tmp_path, capsys):
    class ExplodingHandle:
        def __init__(self, path):
            self._real = open(path, "ab")
            self.writes = 0

        def write(self, data):
            self.writes += 1
            if self.writes > 1:
                raise OSError(28, "No space left on device")
            return self._real.write(data)

        def flush(self):
            self._real.flush()

        def close(self):
            self._real.close()

    made = []

    def opener(path, mode):
        handle = ExplodingHandle(path)
        made.append(handle)
        return handle

    clock = FakeClock()
    spawn, _ = chatty_spawn(clock)

    result = supervised_runner(
        tmp_path,
        spawn,
        clock,
        action_log_dir=tmp_path / "logs",
        action_log_opener=opener,
    ).run(SPEC)

    assert result.ok
    assert result.raw_text == "done"
    assert made and made[0].writes > 1  # it really did try again and really did fail
    assert "No space left on device" in capsys.readouterr().err


def test_an_agent_that_never_starts_still_leaves_a_log_that_says_so(tmp_path, capsys):
    """A file holding only a header is indistinguishable from a run that
    printed nothing. The failure goes IN the file."""

    def stub(argv, **kwargs):
        raise FileNotFoundError("claude")

    log_dir = tmp_path / "logs"
    result = ClaudeCliRunner(tmp_path, runner=stub, action_log_dir=log_dir).run(SPEC)

    assert not result.ok
    assert result.error.startswith("agent command not found")
    assert "did not complete" in only_log(log_dir).read_text()


def test_a_domain_that_looks_like_a_path_cannot_escape_the_log_directory(tmp_path):
    clock = FakeClock()
    spawn, _ = chatty_spawn(clock)
    log_dir = tmp_path / "logs"

    supervised_runner(tmp_path, spawn, clock, action_log_dir=log_dir).run(
        AgentSpec(domain="../../escaped", title="t", prompt="p")
    )

    written = only_log(log_dir)
    assert written.parent == log_dir
    assert "escaped" in written.name
    assert not (tmp_path.parent / "escaped.log").exists()


def test_a_domain_with_nothing_usable_in_it_still_names_a_file():
    assert action_log_slug("../..") == "agent"
    assert action_log_slug("") == "agent"
    assert action_log_slug("...") == "agent"
    assert action_log_slug(None) == "None"
    assert "/" not in action_log_slug("a/b/c")
    assert len(action_log_slug("z" * 500)) <= 80


# ---- 4. the cap truncates, and says so IN the file ---------------------------


def test_the_cap_truncates_and_records_that_it_did(tmp_path):
    clock = FakeClock()
    # 'Q' because the cap counts AGENT output only: the file's own header sits
    # on top of it, so a payload character that also occurs in the header would
    # make the count say the bound leaked when it had not.
    spawn, _ = chatty_spawn(clock, exit_at=60.0, per_poll=b"Q" * 200)
    log_dir = tmp_path / "logs"

    supervised_runner(
        tmp_path,
        spawn,
        clock,
        action_log_dir=log_dir,
        action_log_max_bytes=300,
    ).run(SPEC)

    body = only_log(log_dir).read_text()
    assert "TRUNCATED" in body
    assert "300 bytes" in body
    # The bound really binds: the agent produced far more than the cap, and
    # exactly the cap's worth of it is here.
    assert body.count("Q") == 300


def test_truncation_is_recorded_in_the_file_not_only_on_stderr(tmp_path):
    """"A log that lies about being complete is worse than a short one" — so
    the notice has to be in the artefact, where whoever reads the log later
    will see it, not only in a stream nobody kept."""
    path = tmp_path / "logs" / "one.log"
    log = open_action_log(path, max_bytes=10)
    log.write("stdout", b"0123456789ABCDEF")
    log.close()

    assert log.truncated
    assert "TRUNCATED" in path.read_text()
    assert path.read_text().startswith("\n[stdout]\n0123456789")


def test_the_log_never_says_it_was_written_after_the_cap(tmp_path):
    path = tmp_path / "logs" / "one.log"
    log = open_action_log(path, max_bytes=4)
    log.write("stdout", b"AAAA")
    log.write("stdout", b"BBBB")
    log.close()

    assert "BBBB" not in path.read_text()


# ---- 5. it is an ACTION log, never a thinking stream --------------------------


def test_nothing_the_log_shows_calls_itself_the_model_thinking(tmp_path):
    clock = FakeClock()
    spawn, _ = chatty_spawn(clock, exit_at=20.0)
    log_dir = tmp_path / "logs"

    supervised_runner(tmp_path, spawn, clock, action_log_dir=log_dir).run(SPEC)

    written = only_log(log_dir)
    production_dir = AutoloopConfig(
        browser=BrowserConfig(), policy=PolicyConfig(), state_dir=tmp_path
    ).action_log_dir
    # The three surfaces that carry a name to an operator: the file's own
    # bytes, its file name, and the directory production puts it in. `log_dir`
    # itself is a pytest tmp path and would only grade pytest's naming.
    surfaces = [written.read_text().lower(), written.name.lower(), production_dir.name.lower()]
    for surface in surfaces:
        for word in THINKING_WORDS:
            assert word not in surface, f"{word!r} appears in {surface[:120]!r}"
    assert "action log" in written.read_text().lower()


def test_the_buffered_path_says_it_was_not_streamed(tmp_path):
    """The unsupervised runner hands over its output in one piece, after the
    process has exited. Saying so closes the fail-open where a file that
    appeared at the end reads as a stream that recorded nothing."""

    def stub(argv, **kwargs):
        class Proc:
            returncode = 0
            stdout = json.dumps({"result": "audited"})
            stderr = "a warning\n"

        return Proc()

    log_dir = tmp_path / "logs"
    ClaudeCliRunner(tmp_path, runner=stub, action_log_dir=log_dir).run(SPEC)

    body = only_log(log_dir).read_text()
    assert "NOT STREAMED" in body
    assert ACTION_LOG_BUFFERED_NOTICE.strip() in body
    assert "a warning" in body


# ---- 6. one file per round, and rounds never overwrite each other ------------


def test_two_rounds_of_one_task_write_two_files(tmp_path):
    """`ImplementExecutor._bindings_for` builds a fresh runner from the
    configured factory on every `execute()`, so one runner is one round — and
    two runners must not land on one file however fast they follow each other."""
    log_dir = tmp_path / "logs"
    for _ in range(2):
        clock = FakeClock()
        spawn, _box = chatty_spawn(clock, exit_at=20.0)
        supervised_runner(tmp_path, spawn, clock, action_log_dir=log_dir).run(SPEC)

    assert len(sorted(log_dir.glob("*.log"))) == 2


def test_repeated_runs_inside_one_round_append_to_one_file(tmp_path):
    """The advisory validation rendezvous re-runs the agent inside one round.
    Those runs are one round and belong in one file."""
    log_dir = tmp_path / "logs"
    clock = FakeClock()
    spawn, _ = chatty_spawn(clock, exit_at=20.0, per_poll=b"round-output\n")
    runner = supervised_runner(tmp_path, spawn, clock, action_log_dir=log_dir)

    runner.run(SPEC)
    clock.now = 0.0  # the same spawn hands back a fresh process for the re-run
    runner.run(SPEC)

    assert len(sorted(log_dir.glob("*.log"))) == 1
    assert only_log(log_dir).read_text().count("autoloop action log") == 2


def test_the_round_stamp_is_distinct_per_round():
    assert action_log_round_stamp() != action_log_round_stamp()
    assert "/" not in action_log_round_stamp()


# ---- 7. the production executor path ------------------------------------------


def _init_repo(root: Path, branch: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    make_repo_from_template(
        root, branch=branch, files=(("README.md", "hi"),), email="t@e.c", name="T"
    )
    return root


@pytest.fixture
def main_repo(tmp_path):
    return _init_repo(tmp_path / "main", "main")


@pytest.fixture
def worker_repo(tmp_path):
    return _init_repo(tmp_path / "worker", "autoloop/t1")


def write_config(tmp_path, *, action_log=None):
    """A real config file for `load_config`, with `[audit] action_log` written
    exactly as an operator would write it (or ABSENT, which is every config
    file that predates the key)."""
    workers_root = tmp_path / "workers"
    state_dir = tmp_path / "state"
    body = [
        "[paths]",
        f'workers_root = "{workers_root}"',
        f'state_dir = "{state_dir}"',
    ]
    if action_log is not None:
        body += ["[audit]", f"action_log = {str(bool(action_log)).lower()}"]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(body) + "\n")
    return path


def wired_action_log_dir(config, main_repo, policy, tmp_path):
    """The directory the REAL `cli._build_executor` gave the runner it built for
    a task, read back off that runner.

    The two end-to-end rounds below learn the operator's setting through this
    and through nothing else. Recomputing `config.action_log_dir if
    config.audit.action_log else None` in the test would be the same expression
    production uses and would still prove only that the test can spell it — the
    round would pass against a `_build_executor` that passes nothing at all.
    Taking the value from production's own runner makes it one link instead of
    two.

    The runner is discarded: it holds the real `abort_aware_spawn` and would
    launch an actual `claude`. Only the directory it was configured with is
    used, by a runner the round builds with the process faked.
    """
    wired = build_production_executor(config, main_repo, policy, tmp_path)
    return wired._implement._agent_runner_factory(main_repo)._action_log_dir


def production_factory(root, *, action_log_dir, spawn, clock, stall_policy, policy):
    """EXACTLY the call `cli._build_executor`'s `agent_runner_factory` makes,
    minus the abort switch and with the process faked.

    `action_log_dir=` is passed because production passes it — that IS the
    wiring. Both callers below get the value from `wired_action_log_dir`, i.e.
    off the runner the real `cli._build_executor` built, so no test here
    chooses it.
    """
    return implement_agent_runner(
        root,
        policy=policy,
        stall_policy=stall_policy,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        action_log_dir=action_log_dir,
    )


def build_production_executor(config, main_repo, policy, tmp_path):
    """`cli._build_executor` itself, with the collaborators it really takes.

    The decision under test is inside that function, so nothing here stands in
    for it: a runner assembled by hand, or a check that reads `cli.py` as text,
    would pass just as happily against a `_build_executor` that never mentions
    the action log at all. That is how stream-01 shipped an inert flag four
    times.
    """
    return cli._build_executor(
        config,
        argparse.Namespace(null_executor=False),
        GitGateway(main_repo, policy),
        TaskRegistry(),
        WorkerRepoManager(config.workers_root, tmp_path / "hooks"),
        policy,
    )


def test_a_config_with_the_flag_on_names_the_directory_under_the_state_dir(tmp_path):
    config = load_config(write_config(tmp_path, action_log=True))

    assert config.audit.action_log is True
    assert config.action_log_dir == tmp_path / "state" / "action-logs"


def test_the_cli_passes_the_configured_directory_to_the_production_runner(
    main_repo, worker_repo, tmp_path
):
    """THE wire, asked at the site that carries it.

    `cli._build_executor` is the only production caller that has both the
    operator's flag and the runner construction sites in one scope, so this is
    where "the flag reaches a real round" is either true or false. The runner
    asserted on is the one `ImplementExecutor._bindings_for` will build for a
    task — obtained by calling the executor's own factory, not by rebuilding
    something that resembles it.
    """
    config = load_config(write_config(tmp_path, action_log=True))
    policy = PolicyEngine(PolicyConfig())

    executor = build_production_executor(config, main_repo, policy, tmp_path)
    runner = executor._implement._agent_runner_factory(worker_repo)

    assert runner._action_log_dir == config.action_log_dir
    # And it really is the runner a round runs: write-capable, and SUPERVISED,
    # which is what makes the file a live stream rather than a dump written
    # once the process has already exited.
    assert runner._allowed_tools == WRITE_ALLOWED_TOOLS
    assert runner._disallowed_tools == IMPLEMENT_DISALLOWED_TOOLS
    assert runner._progress_probe is not None
    # The standalone binding agrees. It is never reached (the factory wins
    # whenever `worker_repo_root_for` is set, which it always is here), but two
    # write-capable sites disagreeing about the operator's setting would read as
    # a decision rather than as the dead code it is.
    assert executor._implement._agent_runner._action_log_dir == config.action_log_dir


def test_the_cli_passes_no_directory_to_the_production_runner_with_the_flag_off(
    main_repo, worker_repo, tmp_path
):
    """The other half, at the same site: OFF means the runner is handed `None`,
    which is what it was handed before this parameter existed."""
    config = load_config(write_config(tmp_path, action_log=False))
    policy = PolicyEngine(PolicyConfig())

    executor = build_production_executor(config, main_repo, policy, tmp_path)

    assert executor._implement._agent_runner_factory(worker_repo)._action_log_dir is None
    assert executor._implement._agent_runner._action_log_dir is None


def test_the_cli_never_gives_the_read_only_audit_runners_a_log(
    main_repo, worker_repo, tmp_path
):
    """With the flag ON, and deliberately.

    An audit subagent is `subprocess.run(capture_output=True)`: its output does
    not exist until the process has exited, so a file opened for it could only
    ever be written at the end — a log that reads like the live stream this
    setting promises and is not one. Both audit construction sites are asked,
    because the factory is the one a real audit round uses.
    """
    config = load_config(write_config(tmp_path, action_log=True))
    policy = PolicyEngine(PolicyConfig())

    executor = build_production_executor(config, main_repo, policy, tmp_path)

    assert executor._audit._agent_runner._action_log_dir is None
    assert executor._audit._agent_runner_factory(worker_repo)._action_log_dir is None


def test_loading_a_config_changes_nothing_outside_the_object_it_returns(tmp_path):
    """The wire is an ARGUMENT, and this is what that buys.

    `load_config` used to arm a process-wide default, which made every runner
    built afterwards depend on which config the process had read last — a
    setting no reader of a construction site could predict, and one that leaked
    between tests. Reading a `true` config must now leave a runner that was
    handed nothing exactly as off as it was before.
    """
    load_config(write_config(tmp_path, action_log=True))
    clock = FakeClock()
    spawn, _ = chatty_spawn(clock, exit_at=20.0, per_poll=b"tool: Read(a)\n")

    runner = supervised_runner(tmp_path, spawn, clock)
    result = runner.run(SPEC)

    assert result.ok
    assert runner._action_log_dir is None
    assert not (tmp_path / "state" / "action-logs").exists()


def test_a_directory_that_cannot_be_named_leaves_the_log_off_and_says_so(
    tmp_path, capsys
):
    """Observability may never stop work, and construction is the one place
    left where it could: a `Path()` that refused what it was handed would raise
    out of the runner's `__init__` and the agent would not run at all. So it
    cannot refuse — the log goes off, and stderr says why rather than leaving a
    silently-off log to be discovered later."""
    clock = FakeClock()
    spawn, _ = chatty_spawn(clock, exit_at=20.0)

    runner = supervised_runner(tmp_path, spawn, clock, action_log_dir=object())
    result = runner.run(SPEC)

    assert result.ok
    assert result.raw_text == "done"
    assert runner._action_log_dir is None
    assert "action log" in capsys.readouterr().err


def test_the_production_factory_forwards_the_directory_it_is_given(
    tmp_path, worker_repo
):
    """`implement_agent_runner` is the ONE place a write-capable runner is
    built, so a value that stops here never reaches a round. Asserted with the
    write-capable tool sets alongside, so this is that runner and not some
    other one."""
    config = load_config(write_config(tmp_path, action_log=True))

    runner = implement_agent_runner(
        worker_repo,
        policy=PolicyEngine(PolicyConfig()),
        stall_policy=make_policy(),
        action_log_dir=config.action_log_dir,
    )

    assert runner._action_log_dir == config.action_log_dir
    assert runner._allowed_tools == WRITE_ALLOWED_TOOLS
    assert runner._disallowed_tools == IMPLEMENT_DISALLOWED_TOOLS


def test_the_production_factory_logs_nowhere_when_it_is_given_nothing(
    tmp_path, worker_repo
):
    """The default, which is what every caller that predates the parameter
    gets — `test_validation_env.py` and `test_stall_detector.py` both build one
    this way."""
    runner = implement_agent_runner(
        worker_repo, policy=PolicyEngine(PolicyConfig()), stall_policy=make_policy()
    )

    assert runner._action_log_dir is None


def test_a_production_round_produces_a_non_empty_log_while_it_is_still_running(
    main_repo, worker_repo, tmp_path
):
    """The test stream-01 never had.

    A whole round through `ImplementExecutor.execute()` — the real executor
    entry point, the real `_bindings_for`, the real `implement_agent_runner`
    called the way `cli._build_executor` calls it, the real worker repository,
    the real `WorkerTreeProbe` over real git, the real supervisor — with the
    agent's process faked and nothing else. The only thing that turns the log
    on is `[audit] action_log = true` in a config file `load_config` read, and
    the only thing that carries it here is `cli._build_executor` — the
    directory this round logs to is read back off the runner that function
    built (`wired_action_log_dir`), never recomputed from the config.

    The log is sampled from inside the supervisor's sleep, so a non-empty
    sample is a non-empty log at a moment when the round had not finished.
    """
    config = load_config(write_config(tmp_path, action_log=True))
    log_dir = config.action_log_dir
    clock = FakeClock()
    policy = PolicyEngine(PolicyConfig())
    action_log_dir = wired_action_log_dir(config, main_repo, policy, tmp_path)
    assert action_log_dir == log_dir, "cli._build_executor wired a different directory"
    stall_policy = make_policy(stall=600.0, ceiling=6000.0, poll=10.0)
    box = {}
    samples = []

    def spawn(argv, *, cwd, env, stdout, stderr):
        # What a real agent's Write tool does, so the round has a change to
        # commit and the tree probe has progress to see.
        (Path(cwd) / "feature.py").write_text("x = 1\n")
        handle = ChattyHandle(
            clock,
            stdout,
            stderr,
            exit_at=40.0,
            per_poll=b"tool: Write(feature.py)\n",
            result=json.dumps({"result": "implemented the widget"}).encode(),
        )
        box["handle"] = handle
        return handle

    def watching_sleep(seconds):
        found = next(iter(log_dir.glob("*.log")), None)
        samples.append(
            (box["handle"].alive, found.read_bytes() if found is not None else b"")
        )
        clock.now += seconds

    clock.sleep = watching_sleep

    executor = ImplementExecutor(
        git=GitGateway(main_repo, policy),
        # The standalone binding, never reached: `worker_repo_root_for` and
        # `agent_runner_factory` are both set, and `_bindings_for` prefers them.
        agent_runner=UnusedRunner(),
        validation_commands=(),
        worker_repo_root_for=lambda task_id: worker_repo,
        policy=policy,
        agent_runner_factory=lambda root: production_factory(
            root,
            action_log_dir=action_log_dir,
            spawn=spawn,
            clock=clock,
            stall_policy=stall_policy,
            policy=policy,
        ),
        advisory_zero_call_returns=0,
    )

    outcome = executor.execute(
        Directive(decision=Decision.IMPLEMENT, reason="r", task_id="t1"),
        Task(id="t1", title="Add widget", description="Implement the widget feature."),
    )

    assert outcome.status == "ok", outcome.summary
    assert "feature.py" in outcome.changed_paths
    live = [body for alive, body in samples if alive and b"tool: Write(feature.py)" in body]
    assert live, (
        "a production round through ImplementExecutor.execute() left the action "
        "log empty for the whole time the agent process was alive; samples were "
        f"{[(a, len(b)) for a, b in samples]}"
    )
    written = only_log(log_dir)
    assert written.name.startswith("t1-")  # named by the task
    assert "tool: Write(feature.py)" in written.read_text()
    # OUTSIDE the observed tree (port-01). A log written inside the worker
    # repository is the agent writing where it may not, as far as
    # `escape_detector` is concerned — a `loop_fatal` park caused by watching
    # the round.
    assert not written.is_relative_to(worker_repo)
    assert not written.is_relative_to(main_repo)


def test_the_same_production_round_with_the_log_off_writes_nothing(
    main_repo, worker_repo, tmp_path, capsys
):
    """The other half of the claim, and the same round: with the setting OFF —
    which is what a config that never mentions it gives, i.e. every config file
    that predates the key — the whole executor path behaves as it did before
    this existed. Same executor, same factory, same fake process as the test
    above; only the config file differs."""
    config = load_config(write_config(tmp_path, action_log=False))
    log_dir = config.action_log_dir
    clock = FakeClock()
    policy = PolicyEngine(PolicyConfig())
    action_log_dir = wired_action_log_dir(config, main_repo, policy, tmp_path)
    assert action_log_dir is None, "cli._build_executor wired a log with the flag off"

    def spawn(argv, *, cwd, env, stdout, stderr):
        (Path(cwd) / "feature.py").write_text("x = 1\n")
        return ChattyHandle(
            clock,
            stdout,
            stderr,
            exit_at=40.0,
            per_poll=b"noise\n",
            result=json.dumps({"result": "implemented the widget"}).encode(),
        )

    executor = ImplementExecutor(
        git=GitGateway(main_repo, policy),
        # The standalone binding, never reached: `worker_repo_root_for` and
        # `agent_runner_factory` are both set, and `_bindings_for` prefers them.
        agent_runner=UnusedRunner(),
        validation_commands=(),
        worker_repo_root_for=lambda task_id: worker_repo,
        policy=policy,
        agent_runner_factory=lambda root: production_factory(
            root,
            action_log_dir=action_log_dir,
            spawn=spawn,
            clock=clock,
            stall_policy=make_policy(stall=600.0, ceiling=6000.0, poll=10.0),
            policy=policy,
        ),
        advisory_zero_call_returns=0,
    )

    outcome = executor.execute(
        Directive(decision=Decision.IMPLEMENT, reason="r", task_id="t1"),
        Task(id="t1", title="Add widget", description="Implement the widget feature."),
    )

    assert outcome.status == "ok", outcome.summary
    assert not log_dir.exists()
    assert "action log" not in capsys.readouterr().err


# ---- 8. the pump reads without moving the process's own offset ---------------


def test_reading_the_log_source_with_pread_does_not_move_the_writer(tmp_path):
    """Why the pump uses `os.pread` and never `seek`.

    `subprocess.Popen(stdout=<file>)` hands the child a `dup2` of the parent's
    descriptor, and duplicated descriptors SHARE one file offset — that is what
    `os.dup` models exactly, and it is the whole hazard. The first half shows
    `pread` leaving the writer where it was; the second half shows a plain
    `seek` moving the position the writer is about to use, which is the agent's
    output overwriting itself.
    """
    with tempfile.TemporaryFile() as out:
        child = os.dup(out.fileno())  # the descriptor the agent process writes on
        try:
            os.write(child, b"line0\n")
            assert os.pread(out.fileno(), 4096, 0) == b"line0\n"  # the pump reads
            os.write(child, b"line1\n")  # the agent writes again
        finally:
            os.close(child)
        out.seek(0)
        assert out.read() == b"line0\nline1\n"

    with tempfile.TemporaryFile() as out:
        child = os.dup(out.fileno())
        try:
            os.write(child, b"line0\n")
            out.seek(0)  # a naive pump, seeking to its stored offset
            os.write(child, b"XXXXXX")
        finally:
            os.close(child)
        out.seek(0)
        assert out.read() == b"XXXXXX", "the seek really does corrupt the writer"


@pytest.mark.parametrize("cap", [0, 1, DEFAULT_ACTION_LOG_MAX_BYTES])
def test_the_writer_survives_every_cap(tmp_path, cap):
    path = tmp_path / f"cap-{cap}.log"
    log = open_action_log(path, max_bytes=cap)
    log.note("# header\n")
    log.write("stdout", b"hello")
    log.close()

    assert path.exists()
    assert "# header" in path.read_text()
