"""One status view across every configured project (port-04).

Four loops running blind is not viable, and the failure this view exists to
prevent is a STOPPED LOOP GOING UNNOTICED. So the tests that matter are the ones
proving the view keeps telling the truth when one project misbehaves:

* a project whose config cannot be read is reported `unknown`, with the reason,
  while every other project still renders — one bad config must never blank the
  page;
* a project with an open `task_fatal` blocker and a LIVE lock is not reported as
  stopped. `health` says `blocked` whenever any blocker is open, including while
  continuous mode works other tasks on that project, and a reader that conflated
  the two raised a false parked alarm and sent a needless email on 2026-08-15;
* looking at a project writes NOTHING — proven against a byte-for-byte snapshot
  of the checkouts and the state directories. Four incidents on 2026-08-15/16
  were a dashboard restart and a health poller compiling `__pycache__` inside a
  live checkout, which the escape detector correctly reported as a
  worker-isolation escape: loop-fatal, one reset and the in-flight round each.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autoloop import cli, dashboard, health
from autoloop.blockers import BlockerStore
from autoloop.config import ConfigError, load_config
from autoloop.lock import LoopLock
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


# --- fixtures: real deployments on disk ---------------------------------------


class Project:
    """One project's directories, exactly as a real deployment lays them out:
    a checkout, a state dir beside it, a workers root, and a config file naming
    the last two."""

    def __init__(self, root: Path, name: str):
        self.name = name
        self.home = root / f"{name}-home"
        # The checkout is NAMED after the project, as a real one is: the label
        # on the row comes from the directory holding `.autoloop/`.
        self.checkout = self.home / name
        self.state_dir = self.home / "state"
        self.workers_root = self.home / "workers"
        self.config_path = self.checkout / ".autoloop" / "config.toml"

        (self.checkout / ".autoloop").mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        self.workers_root.mkdir(parents=True)
        # A real .py file inside the observed checkout, so that an accidental
        # import of the project's own vendored package would leave a
        # `__pycache__` behind for the snapshot test to catch. Without it that
        # test would pass for the wrong reason.
        package = self.checkout / "autoloop"
        package.mkdir()
        (package / "__init__.py").write_text("VERSION = '1'\n", encoding="utf-8")

        self.config_path.write_text(
            "\n".join(
                [
                    "[paths]",
                    f'state_dir = "{self.state_dir}"',
                    f'workers_root = "{self.workers_root}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

    @property
    def config(self):
        return load_config(self.config_path)

    def transcript(self, minutes_ago: float = 2.0, base: datetime | None = None) -> None:
        """`base` is `NOW` for every test that pins a clock, and the REAL clock
        for the two that go through `cli.main`, which reads `datetime.now`."""
        stamp = ((base or NOW) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        (self.state_dir / "transcript.jsonl").write_text(
            json.dumps({"ts": stamp, "type": "directive"}) + "\n", encoding="utf-8"
        )

    def state(self, **kw) -> None:
        kw.setdefault("phase", Phase.EXECUTING.value)
        StateStore(self.state_dir / "state.json").save(
            LoopState(session_id=self.name, conversation_url="", **kw)
        )

    def blocker(self, *, code="approved_paths_missing", kind="task_fatal", task_id="t-1"):
        return BlockerStore(self.state_dir / "blockers").record(
            task_id=task_id,
            kind=kind,
            code=code,
            question=f"{task_id} needs a decision",
            detail="",
            phase="executing",
            now=NOW.isoformat(timespec="seconds"),
        )

    def registry(self, tasks) -> None:
        TaskStore(self.state_dir / "tasks.json").save(TaskRegistry(list(tasks)))

    def lock(self) -> LoopLock:
        """A REAL lock held by this process — `LoopLock.is_live` then answers
        `True` on its own evidence rather than on a stub."""
        return LoopLock(self.state_dir)


@pytest.fixture
def project(tmp_path):
    def make(name: str) -> Project:
        return Project(tmp_path, name)

    return make


def _awake(start, end):
    return health.SleepEvidence(0.0, "awake throughout (test)")


def _checker(config, now):
    """`health.check` with deterministic probes — the REAL verdict logic, but
    not the test host's process table or its sysctls."""
    return health.check(config, now=now, agent_probe=lambda: False, sleep_probe=_awake)


def _status(paths, checker=_checker):
    return dashboard.projects_status([str(p) for p in paths], now=NOW, checker=checker)


def _row_line(text: str, label: str) -> str:
    """The one TABLE line for `label`.

    A row is `<marker> <label> <cells...>`; the reason lines under it are
    indented six spaces, so splitting on whitespace and taking the first token
    tells them apart (a reason line's first token is never the bare label
    because it is preceded by the indent, which `split()` drops — hence the
    explicit check that the line is not indented).
    """
    matches = [
        line
        for line in text.splitlines()
        if not line.startswith(" " * 6) and line.replace("!", "", 1).split()[:1] == [label]
    ]
    assert len(matches) == 1, f"expected exactly one row for {label!r} in:\n{text}"
    return matches[0]


# --- one bad project must not blank the view ----------------------------------


def test_an_unreadable_config_is_unknown_and_the_others_still_render(project):
    """THE isolation requirement. A misconfigured project is a row saying so,
    never an exception that takes the page down."""
    good_a, broken, good_b = project("alpha"), project("beta"), project("gamma")
    for proj in (good_a, good_b):
        proj.transcript()
        proj.state()
    broken.config_path.write_text("this is not = valid = toml", encoding="utf-8")

    with good_a.lock(), good_b.lock():
        rows = _status([good_a.config_path, broken.config_path, good_b.config_path])
    text = dashboard.render_projects_text(rows)

    assert [row.label for row in rows] == ["alpha", "beta", "gamma"], "configured order"
    assert rows[1].code == health.STUCK_UNKNOWN
    assert rows[1].needs_attention is True, "a loop nobody can see is not a loop that is fine"
    assert rows[1].loop_state == dashboard.PROJECT_LOOP_UNKNOWN, "no lock was read"
    assert "beta" in rows[1].detail or "beta" in "".join(rows[1].notes), "names the file"
    # The other two are unaffected — verdicts AND rendering.
    assert rows[0].code == health.OK_RUNNING and rows[2].code == health.OK_RUNNING
    for label in ("alpha", "beta", "gamma"):
        assert label in text
    assert health.STUCK_UNKNOWN in _row_line(text, "beta")


def test_a_config_path_that_does_not_exist_is_unknown(project):
    """The likeliest misconfiguration of all: a path with a typo in it."""
    good = project("alpha")
    good.transcript()
    good.state()

    with good.lock():
        rows = _status([good.config_path, good.home / "nope" / "config.toml"])

    assert rows[1].code == health.STUCK_UNKNOWN
    assert rows[1].needs_attention is True
    assert "config file not found" in rows[1].detail
    assert rows[0].code == health.OK_RUNNING


def test_a_project_whose_config_path_is_a_directory_is_unknown(project):
    """Not a `ConfigError`: `read_text` on a directory raises `IsADirectoryError`,
    which is why the guard is broad rather than a tuple of config exceptions."""
    good = project("alpha")
    good.transcript()
    good.state()

    with good.lock():
        rows = _status([good.config_path, good.home])

    assert rows[1].code == health.STUCK_UNKNOWN
    assert rows[0].code == health.OK_RUNNING


def test_a_health_check_that_raises_leaves_the_facts_standing(project):
    """The verdict is the part that can fail. Losing it must not lose whether
    the lock is live — that is what the operator acts on."""
    proj = project("alpha")
    proj.transcript()
    proj.state()

    def boom(config, now):
        raise RuntimeError("the verdict exploded")

    with proj.lock():
        rows = _status([proj.config_path], checker=boom)

    assert rows[0].code == health.STUCK_UNKNOWN
    assert rows[0].needs_attention is True
    assert rows[0].loop_state == dashboard.PROJECT_LIVE, "the fact survived the verdict"
    assert rows[0].stopped is False
    assert "the verdict exploded" in rows[0].detail


# --- blocked-while-running is not stopped -------------------------------------


def test_an_open_task_fatal_blocker_with_a_live_lock_is_not_stopped(project):
    """The 2026-08-15 false alarm, pinned. `health` says `blocked` because a
    blocker IS open, and that is correct — but continuous mode is working other
    tasks on that project meanwhile, so the loop is up."""
    proj = project("alpha")
    proj.transcript()
    proj.state()
    proj.blocker(kind="task_fatal")

    with proj.lock():
        rows = _status([proj.config_path])
        text = dashboard.render_projects_text(rows)

    row = rows[0]
    assert row.code == health.STUCK_BLOCKED, "the verdict vocabulary is unchanged"
    assert row.loop_state == dashboard.PROJECT_LIVE
    assert row.stopped is False
    assert row.open_blockers == 1
    # The RENDERED line is what the operator reads, and a reader conflating the
    # two is the failure being fixed — so the reader is what gets pinned.
    line = _row_line(text, "alpha")
    assert dashboard.PROJECT_LIVE in line
    # Pinned on the ROW and on the footer's own phrasing rather than on
    # `"stopped" not in text`: that blanket reading passes here only because this
    # fixture has one project, and would start failing the moment a genuinely
    # down project were added beside this one — for the right reason, which is
    # exactly what makes it the wrong assertion for this claim.
    assert dashboard.PROJECT_STOPPED not in line
    assert f"STOPPED: {row.label}" not in text, "the footer must not name it either"


def test_a_loop_that_is_actually_down_says_stopped(project):
    """The other half of the same distinction: no lock, no pause flag."""
    proj = project("alpha")
    proj.transcript()
    proj.state()

    rows = _status([proj.config_path])
    text = dashboard.render_projects_text(rows)

    assert rows[0].loop_state == dashboard.PROJECT_STOPPED
    assert rows[0].stopped is True
    assert rows[0].code == health.STUCK_NOT_RUNNING
    assert "STOPPED: alpha" in text, "the footer names it"


def test_a_paused_loop_is_not_reported_stopped(project):
    """A pause is a decision, not a fault — `health` already says so, and the
    loop-state fact must not contradict it."""
    proj = project("alpha")
    proj.transcript()
    proj.state()
    proj.config.pause_file.write_text("", encoding="utf-8")

    rows = _status([proj.config_path])

    assert rows[0].loop_state == dashboard.PROJECT_PAUSED
    assert rows[0].stopped is False
    assert rows[0].code == health.OK_PAUSED
    assert rows[0].needs_attention is False


def test_a_blocked_project_whose_loop_is_down_is_stopped_and_blocked(project):
    """Both facts at once, which is the whole reason they are separate fields."""
    proj = project("alpha")
    proj.transcript()
    proj.state()
    proj.blocker()

    rows = _status([proj.config_path])

    assert rows[0].code == health.STUCK_BLOCKED
    assert rows[0].loop_state == dashboard.PROJECT_STOPPED
    assert rows[0].stopped is True


# --- the numbers on the row ---------------------------------------------------


def test_a_stale_lock_still_reports_the_open_blockers(project, monkeypatch):
    """`health._judge` returns `stale_lock` BEFORE it reads blockers, so
    `Health.open_blockers` is 0 while blockers are open. This view counts them
    itself, because that verdict is exactly where an operator most needs to know
    a decision is also waiting."""
    proj = project("alpha")
    proj.transcript()
    proj.state()
    proj.blocker()

    with proj.lock():
        # Patched INSIDE the hold: `acquire` never consults `is_live` for a lock
        # file that does not exist yet, and `release` does not consult it at
        # all, so this makes a REAL lock file read as stale without stubbing the
        # lock away.
        monkeypatch.setattr(LoopLock, "is_live", staticmethod(lambda info: False))
        rows = _status([proj.config_path])
        verdict = _checker(proj.config, NOW)

    assert verdict.code == health.STUCK_STALE_LOCK and verdict.open_blockers == 0
    assert rows[0].code == health.STUCK_STALE_LOCK
    assert rows[0].open_blockers == 1, "the fail-open this field exists to close"


def test_blockers_that_cannot_be_counted_are_unknown_not_zero(project):
    """"Could not count" and "none open" must not be the same answer.

    A corrupt blocker record raises by design (`BlockerStore.load` refuses to
    read one as absent), which also takes `health.check` down — so this pins
    BOTH halves: the row still renders, with the count as unknown rather than 0.
    """
    proj = project("alpha")
    proj.transcript()
    proj.state()
    blocker = proj.blocker()
    (proj.state_dir / "blockers" / f"{blocker.id}.json").write_text(
        "{not json", encoding="utf-8"
    )

    rows = _status([proj.config_path])
    text = dashboard.render_projects_text(rows)

    assert rows[0].open_blockers is None
    assert rows[0].needs_attention is True, "a fact that could not be read escalates"
    assert any("blocker" in note for note in rows[0].notes)
    assert rows[0].code == health.STUCK_UNKNOWN, "health could not judge it either"
    assert rows[0].loop_state == dashboard.PROJECT_STOPPED, "the facts survived"
    assert "0" not in _row_line(text, "alpha").split(), "never rendered as 0"


def test_silence_with_no_transcript_reads_unknown_not_zero(project):
    """A loop that has never written a transcript is not a loop that just
    wrote one. `0m` there reads as "just active", which is the fail-open."""
    proj = project("alpha")
    proj.state()

    rows = _status([proj.config_path])

    assert rows[0].silent_minutes is None
    assert "—" in _row_line(dashboard.render_projects_text(rows), "alpha")


def test_the_row_carries_silence_the_task_and_the_last_completion(project):
    """The five questions the view exists to answer, on one row."""
    proj = project("alpha")
    proj.transcript(minutes_ago=7)
    proj.state(current_task={"task_id": "port-04", "started_at": NOW.isoformat()})
    proj.registry(
        [
            Task(
                id="dash-05",
                title="older",
                description="the completion before last",
                status="completed",
                completed_at=(NOW - timedelta(hours=30)).isoformat(timespec="seconds"),
            ),
            Task(
                id="halt-01",
                title="newest",
                description="the last thing this loop finished",
                status="completed",
                completed_at=(NOW - timedelta(hours=3)).isoformat(timespec="seconds"),
            ),
            Task(
                id="port-04",
                title="running",
                description="the task the loop says it is on",
                status="in_progress",
            ),
        ]
    )

    with proj.lock():
        rows = _status([proj.config_path])
    row = rows[0]

    assert row.loop_state == dashboard.PROJECT_LIVE
    assert row.current_task == "port-04"
    assert row.phase == Phase.EXECUTING.value
    assert row.silent_minutes == pytest.approx(7.0, abs=0.1)
    assert row.open_blockers == 0
    assert row.last_completed_task == "halt-01", "newest completion, not the last row"
    assert row.last_completed_hours == pytest.approx(3.0, abs=0.1)


def test_an_unreadable_registry_notes_it_instead_of_saying_never(project):
    proj = project("alpha")
    proj.transcript()
    proj.state()
    (proj.state_dir / "tasks.json").write_text("{not json", encoding="utf-8")

    rows = _status([proj.config_path])

    assert rows[0].last_completed_at == ""
    assert rows[0].last_completed_hours is None
    assert any("registry" in note for note in rows[0].notes)
    assert rows[0].needs_attention is True


def test_a_project_that_has_finished_nothing_says_so_quietly(project):
    """An empty registry is not a fault — a new project has landed nothing."""
    proj = project("alpha")
    proj.transcript()
    proj.state()
    proj.registry([Task(id="t-1", title="pending", description="queued, never run")])

    with proj.lock():
        rows = _status([proj.config_path])

    assert rows[0].last_completed_task == ""
    assert rows[0].notes == ()
    assert rows[0].needs_attention is False


# --- writes nothing, anywhere -------------------------------------------------


def _snapshot(root: Path) -> dict:
    """Every path under `root`, with its bytes and its mtime.

    Directories are recorded too, so a `__pycache__` that appears empty (or a
    file created and removed within the window, leaving a changed directory
    mtime) is still caught.
    """
    snap: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        stat = path.lstat()
        if path.is_dir() and not path.is_symlink():
            snap[rel] = ("dir", stat.st_mtime_ns)
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
            snap[rel] = ("file", stat.st_mtime_ns, stat.st_size, digest)
    return snap


def test_the_view_writes_nothing_anywhere(project, tmp_path):
    """Proven against a byte-for-byte snapshot of every checkout, every state
    directory and every workers root, with the REAL `health.check` running and a
    REAL lock held — i.e. the exact shape of looking at a loop mid-round.

    This is the property four incidents on 2026-08-15/16 were caused by
    breaking: a reader that recompiles `__pycache__` inside an observed checkout
    is indistinguishable from a worker-isolation escape, and the loop parks
    loop-fatal on it.
    """
    projects = [project(name) for name in ("alpha", "beta", "gamma")]
    for proj in projects:
        proj.transcript()
        proj.state(current_task={"task_id": "t-1", "started_at": NOW.isoformat()})
        proj.registry(
            [Task(id="t-1", title="one", description="in flight", status="in_progress")]
        )
        proj.blocker(task_id="t-1")
    # A project with NO blocker and a long silence, so the read goes down
    # `health`'s quiet branch and its three probes — `pgrep`, `ps`, `sysctl` —
    # run INSIDE the snapshot window. The three projects above all return
    # `blocked` before that branch is reached, so without this the subprocess
    # path (the one `PYTHONDONTWRITEBYTECODE` on children exists for) would not
    # be covered by this test at all.
    quiet = project("epsilon")
    quiet.transcript(minutes_ago=90)
    quiet.state()
    broken = project("delta")
    broken.config_path.write_text("nonsense = = =", encoding="utf-8")
    # A config naming a state directory that does not exist — the likeliest
    # misconfiguration after a typo'd config path, and the one shape that would
    # expose a read path reaching its answer through `mkdir(exist_ok=True)`.
    # Every store this view touches creates directories only on its WRITE paths;
    # the snapshot below is what pins that rather than a reading of them.
    absent = project("zeta")
    absent.config_path.write_text(
        "\n".join(
            [
                "[paths]",
                f'state_dir = "{absent.home / "never-created"}"',
                f'workers_root = "{absent.workers_root}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    paths = [p.config_path for p in projects] + [
        quiet.config_path,
        broken.config_path,
        absent.config_path,
    ]

    with projects[0].lock(), projects[1].lock(), projects[2].lock(), quiet.lock():
        before = _snapshot(tmp_path)
        # The DEFAULT checker — `health.check` itself, not a stub — so the whole
        # production read path is what the snapshot is taken around.
        rows = dashboard.projects_status([str(p) for p in paths], now=NOW)
        text = dashboard.render_projects_text(rows)
        blob = dashboard.projects_json(rows)
        after = _snapshot(tmp_path)

    assert after == before, "the view must write nothing, anywhere"
    assert [row.label for row in rows] == [
        "alpha",
        "beta",
        "gamma",
        "epsilon",
        "delta",
        "zeta",
    ]
    # The two rows whose READ paths this test added, asserted so a fixture that
    # stopped exercising them would fail here rather than pass quietly.
    assert rows[3].silent_minutes == pytest.approx(90.0, abs=0.1)
    # `silent_minutes` above does NOT prove the quiet branch ran: this view reads
    # it off the transcript itself, before and independently of the verdict, so it
    # would be 90 even if `health` had returned at the blockers check. The
    # verdict's SUMMARY is the discriminating evidence — every outcome of that
    # branch names the silence ("quiet 90m", "no activity for 90 minutes"), while
    # the ordinary live return says "phase=executing" and names none. Asserted as
    # a disjunction because which arm answers depends on the host: a live agent, a
    # busy loop process, unreadable wake history and proven-awake silence are four
    # legitimate outcomes, and all four are past the gate the pgrep/ps/sysctl
    # probes live behind, which is the part this snapshot has to cover.
    assert "quiet" in rows[3].summary or "no activity" in rows[3].summary, (
        "the silence branch, and its subprocess probes, ran inside the snapshot"
    )
    assert rows[5].loop_state == dashboard.PROJECT_STOPPED, "a state dir that is not there"
    assert rows[5].open_blockers == 0, "an absent blocker directory is none open, not a fault"
    assert text and json.loads(blob), "and it did actually produce the view"


def test_observing_disables_bytecode_writes_and_restores_them(project, monkeypatch):
    """The second belt: nothing here imports from an observed checkout, and if
    something ever does, it must not leave a `.pyc` in one. Restored on the way
    out, because a library call must not switch a process-wide flag off
    permanently as a side effect of one read."""
    proj = project("alpha")
    proj.transcript()
    proj.state()
    monkeypatch.delenv(dashboard.BYTECODE_ENV, raising=False)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    seen = {}

    def checker(config, now):
        seen["flag"] = sys.dont_write_bytecode
        seen["env"] = os.environ.get(dashboard.BYTECODE_ENV)
        return _checker(config, now)

    _status([proj.config_path], checker=checker)

    assert seen == {"flag": True, "env": "1"}, "in force while a project is observed"
    assert sys.dont_write_bytecode is False, "and put back afterwards"
    assert dashboard.BYTECODE_ENV not in os.environ

    # The SINGLE-project entry point holds it too, so the guarantee belongs to
    # observing a project rather than to the entry point that happened to be
    # called — and re-entering it from inside the sweep still restores.
    seen.clear()
    dashboard.project_status(proj.config_path, NOW, checker)

    assert seen == {"flag": True, "env": "1"}
    assert sys.dont_write_bytecode is False
    assert dashboard.BYTECODE_ENV not in os.environ


# --- rendering and the empty case ---------------------------------------------


def test_no_projects_configured_is_not_an_empty_table():
    """"No projects" and "four projects, all fine" must never render alike."""
    assert dashboard.render_projects_text(()) == dashboard.NO_PROJECTS_CONFIGURED
    assert "[projects]" in dashboard.NO_PROJECTS_CONFIGURED


def test_two_projects_with_the_same_label_are_told_apart_by_their_paths(tmp_path):
    """Two checkouts named the same thing share a label, and then the column
    identifies neither — so the config path is printed under each of them, and
    only then."""
    one = Project(tmp_path / "a", "app")
    two = Project(tmp_path / "b", "app")
    for proj in (one, two):
        proj.transcript()
        proj.state()

    text = dashboard.render_projects_text(_status([one.config_path, two.config_path]))

    assert text.count(f"      {one.config_path}") == 1
    assert text.count(f"      {two.config_path}") == 1


def test_a_unique_label_does_not_print_its_path(project):
    """The page's value is being scannable; a path under every row halves that."""
    proj = project("alpha")
    proj.transcript()
    proj.state()

    text = dashboard.render_projects_text(_status([proj.config_path]))

    assert str(proj.config_path) not in text


def test_every_code_the_view_reports_is_health_vocabulary(project):
    """One vocabulary, not two. `unknown` is the single word this view adds and
    it lives in `health.py` beside the others."""
    proj = project("alpha")
    proj.transcript()
    proj.state()

    rows = _status([proj.config_path, proj.home / "missing.toml"])

    assert health.STUCK_UNKNOWN in health.VERDICT_CODES
    for row in rows:
        assert row.code in health.VERDICT_CODES


def test_the_json_view_carries_every_field(project):
    proj = project("alpha")
    proj.transcript()
    proj.state()

    rows = _status([proj.config_path])
    payload = json.loads(dashboard.projects_json(rows))

    assert payload[0]["label"] == "alpha"
    assert payload[0]["config_path"] == str(proj.config_path)
    assert payload[0]["loop_state"] == dashboard.PROJECT_STOPPED
    assert payload[0]["code"] == health.STUCK_NOT_RUNNING
    # `stopped` is a PROPERTY, so `asdict` drops it. Carried explicitly, because
    # the reader of this format is the one that would otherwise re-derive the
    # distinction — and deriving it from `code` is the 2026-08-15 false alarm.
    assert payload[0]["stopped"] is True


def test_the_json_view_never_calls_a_blocked_running_loop_stopped(project):
    """The same field, on the row where the two answers disagree."""
    proj = project("alpha")
    proj.transcript()
    proj.state()
    proj.blocker(kind="task_fatal")

    with proj.lock():
        payload = json.loads(dashboard.projects_json(_status([proj.config_path])))

    assert payload[0]["code"] == health.STUCK_BLOCKED
    assert payload[0]["stopped"] is False
    assert payload[0]["loop_state"] == dashboard.PROJECT_LIVE


# --- configuration -------------------------------------------------------------


def test_a_config_with_no_projects_section_still_loads(project):
    """The compatibility contract: every config written before this section
    existed loads unchanged and reports an empty list."""
    proj = project("alpha")

    assert proj.config.projects == ()


def test_the_projects_list_is_read_in_order(project):
    proj = project("alpha")
    other = project("beta")
    proj.config_path.write_text(
        proj.config_path.read_text(encoding="utf-8")
        + "\n[projects]\n"
        + f'configs = ["{other.config_path}", "{proj.config_path}"]\n',
        encoding="utf-8",
    )

    assert proj.config.projects == (other.config_path, proj.config_path)


def test_a_relative_project_path_is_refused(project):
    """It would name a different file for every directory the operator runs the
    command from — the same rule `workers_root` is held to."""
    proj = project("alpha")
    proj.config_path.write_text(
        proj.config_path.read_text(encoding="utf-8")
        + '\n[projects]\nconfigs = ["../other/.autoloop/config.toml"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc:
        proj.config

    assert "absolute" in str(exc.value)


def test_a_blank_project_entry_is_refused(project):
    proj = project("alpha")
    proj.config_path.write_text(
        proj.config_path.read_text(encoding="utf-8") + '\n[projects]\nconfigs = ["  "]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        proj.config


def test_an_unknown_key_in_projects_is_refused(project):
    """Strict config: a typo'd key must never be silently ignored."""
    proj = project("alpha")
    proj.config_path.write_text(
        proj.config_path.read_text(encoding="utf-8")
        + '\n[projects]\nconfig = ["/tmp/x.toml"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc:
        proj.config

    assert "projects" in str(exc.value)


# --- the command ---------------------------------------------------------------


def test_the_command_reports_every_configured_project(project, capsys):
    hub, other = project("alpha"), project("beta")
    for proj in (hub, other):
        proj.transcript()
        proj.state()
    hub.config_path.write_text(
        hub.config_path.read_text(encoding="utf-8")
        + f'\n[projects]\nconfigs = ["{hub.config_path}", "{other.config_path}"]\n',
        encoding="utf-8",
    )

    code = cli.main(["projects", "--config", str(hub.config_path)])
    out = capsys.readouterr().out

    assert code == 1, "neither loop is running, so someone is needed"
    assert "alpha" in out and "beta" in out


def test_the_command_takes_project_paths_directly(project, capsys):
    """So the view works from a machine that runs no loop of its own — no
    `--config`, no `[projects]` section."""
    proj = project("alpha")
    proj.transcript(base=datetime.now(timezone.utc))
    proj.state()

    with proj.lock():
        code = cli.main(["projects", "--project", str(proj.config_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, "one live, healthy loop"
    assert payload[0]["loop_state"] == dashboard.PROJECT_LIVE


def test_the_command_says_when_nothing_is_configured(project, capsys):
    """Exit 2, not 0: an empty view is not "every project is fine"."""
    proj = project("alpha")

    code = cli.main(["projects", "--config", str(proj.config_path)])

    assert code == cli.PROJECTS_UNCONFIGURED_EXIT
    assert dashboard.NO_PROJECTS_CONFIGURED in capsys.readouterr().out
