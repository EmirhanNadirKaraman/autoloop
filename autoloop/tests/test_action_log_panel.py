"""The action log of the running round, on the page (dash-08).

`worker_progress` (dash-07) says whether a round is working — lines written,
minutes elapsed. It cannot say what the round is doing: 400 lines of the wrong
thing and 400 lines of the right thing are the same figure, and the difference
was discoverable only after the round ended, 25 minutes later. This panel
shows the tail of the round's ACTION LOG — the trace of tool calls, files read
and written and commands run — beside those figures, so the two together
answer "is it working, and what is it doing".

Three claims, and the tests are built around the ways each one fails:

* the tail of the CURRENT round's log renders, and only the tail: a long round
  writes a large file and the page polls every two seconds, so the reader is
  bounded in BYTES and never reads the file whole;
* the content is arbitrary text an agent printed and reaches the DOM as text —
  `textContent`, never `innerHTML`, never a template literal — so angle
  brackets and quotes render as the characters they are;
* every absence is a different sentence. No round in flight is no panel, never
  a stale tail. A log that does not exist yet is "nothing written yet"; one
  that exists and could not be read is "could not be read"; and both name the
  path, because an empty box says none of those things.

And a fourth, from the review of round 1: the read CANNOT BLOCK. The path is
looked up once, by a non-blocking open, and what the descriptor turns out to
be is decided by `fstat` on that descriptor — so a fifo at the path, there
before the open or swapped in during it, is refused at once instead of waiting
for a writer on every poll. Those tests run under a watchdog, because a
regression there hangs rather than fails.

THE WRITER IS `audit.agents.ClaudeCliRunner` (stream-01), and it names the
file: `<state_dir>/action-logs/<action_log_slug(task_id)>-<round stamp>.log`,
one per round. Round 1 of this task was cut before the writer landed and
guessed `<task_id>.log`, which matched nothing — a busy round read "nothing
written yet" beside the file it was writing. So `action_log_tail` reads the
path the execution record names (`ACTION_LOG_PATH_FIELD`) and otherwise the
NEWEST of the task's round logs in that directory, and one test below opens a
log through the real writer and asserts the reader finds that file: the
naming cannot drift apart silently again. Every absent state still names
what was looked for, so a mismatch is a visible glob, never an idle agent.

Round 2 closed three more ways the empty states lied, each pinned below:

* `[audit] action_log` is FALSE BY DEFAULT, so a default config beside a
  running round read "nothing written yet" — the sentence that says "wait" —
  for a file that was never coming. `collect()` now reads the setting off the
  config it already reads and an empty directory is `off`, naming the key and
  the file; a config that could not be read claims neither;
* "newest file wins" showed the PREVIOUS round's log as this round's: a
  revision is dispatched with round 1's file already on disk, and until its
  own runner opened a file — or for the whole round, when the writer was on
  then and is off now — the page rendered a stale tail under "this round's
  log". The newest file stamped at or after `current_task.started_at` wins;
  an older one is named and not shown; no parseable stamp means no filter;
* the bounded-read tests measured what `_tail_bytes` RETURNED, which "read
  the whole file and slice" satisfies. They now sum what `os.read` handed
  back on the descriptor the reader opened.

Everything here calls `action_log_tail` with a dict and a `tmp_path` where it
can — the claim is about a file reader and a renderer, and a repository would be
dead weight. One test goes through `collect()`, because the wiring is the one
thing a direct call cannot check. The page's own render runs under node against
a stub document, as `test_dashboard.py`'s merge panel tests do; a structural
check of the script could not tell `textContent` from `innerHTML` with `esc()`
in front of it, and the second is one refactor from the interpolation this
panel must never do. The whole served script is syntax-checked by
`test_dashboard.py::test_the_served_javascript_actually_parses`, which runs on
every change to `dashboard.py`; the harness below executes this panel's region
of it, so a parse failure there fails here too, with node's own message.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gitrepo import make_repo_from_template

from autoloop import dashboard
from autoloop.audit.agents import AgentSpec, ClaudeCliRunner, action_log_slug
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.policy import PolicyConfig
from autoloop.dashboard import (
    ACTION_LOG_DIRNAME,
    ACTION_LOG_EMPTY,
    ACTION_LOG_MISSING,
    ACTION_LOG_OFF,
    ACTION_LOG_OLDER_ROUND,
    ACTION_LOG_PATH_FIELD,
    ACTION_LOG_SETTING,
    ACTION_LOG_SETTING_ON,
    ACTION_LOG_SETTING_UNCHECKED,
    ACTION_LOG_STATES,
    ACTION_LOG_SUFFIX,
    ACTION_LOG_TAIL_BYTES,
    ACTION_LOG_TAIL_LINES,
    ACTION_LOG_UNLOCATABLE,
    ACTION_LOG_UNREADABLE,
    PAGE,
    action_log_tail,
    collect,
)

TASK = "dash-08"

#: When the fixture round was DISPATCHED (`current_task.started_at`, as
#: `utcnow_iso()` writes it). `STAMP` below is the same second, which is the
#: earliest a log of this dispatch can carry — the runner is built after the
#: record is written — so a file named with it is this round's.
DISPATCHED_AT = "2026-09-11T10:00:00+00:00"


@pytest.fixture(autouse=True)
def _clean_dashboard_caches():
    """Same reason as `test_dashboard.py`'s: the tracker memoizes remote refs,
    ancestry verdicts, shallowness and the commit-subject walk at module level,
    and `collect` is called here."""
    caches = (dashboard._REMOTE_CACHE, dashboard._ANCESTRY_CACHE,
              dashboard._SHALLOW_CACHE, dashboard._SUBJECT_CACHE)
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


def running(task_id=TASK, started_at=DISPATCHED_AT, **fields) -> dict:
    """`state.json` shaped like a loop mid-round: a `task_execution` naming the
    unit in flight, which is the one condition every reader of this panel keys
    off, and a `current_task` dated `started_at` — the dispatch time the
    reader filters older rounds' logs by. No `action_log_path` unless the
    caller adds one."""
    return {
        "phase": "executing",
        "current_task": {"task_id": task_id, "started_at": started_at},
        "task_execution": {
            "task_id": task_id, "task_branch": f"autoloop/{task_id}",
            "worktree_path": "/nonexistent/worker", "task_base_sha": "a" * 40,
            "review_round": 0, **fields,
        },
    }


def dispatched_just_now() -> str:
    """A dispatch stamp a minute in the past by the REAL clock, for the tests
    that open a log through the real writer: its stamp is `now`, so a fixed
    date could sit on either side of it depending on when the suite runs."""
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")


#: A round stamp shaped exactly as `agents.action_log_round_stamp()` shapes one
#: (`YYYYmmddTHHMMSS.ffffff-<pid>-<n>`), fixed so names built here are
#: deterministic and in the same second as `DISPATCHED_AT`. The test that pins
#: the SHAPE gets its name from the real writer, not from this.
STAMP = "20260911T100000.000000-4242-0"
#: One second before the dispatch: a log an EARLIER round of the same task
#: left behind, which the reader must name and never show.
EARLIER_STAMP = "20260911T095959.999999-4242-0"


def convention_dir(state_dir) -> Path:
    """The writer's directory: `config.action_log_dir`, i.e. `<state_dir>/action-logs`."""
    return Path(state_dir) / ACTION_LOG_DIRNAME


def convention_path(state_dir, task_id=TASK, stamp=STAMP) -> Path:
    """Where the writer puts ONE round's log for `task_id`."""
    return convention_dir(state_dir) / f"{action_log_slug(task_id)}-{stamp}{ACTION_LOG_SUFFIX}"


def looked_for(state_dir, task_id=TASK) -> str:
    """The glob the `missing` sentence names when no round log was found."""
    return str(convention_dir(state_dir) / f"{action_log_slug(task_id)}-*{ACTION_LOG_SUFFIX}")


def write_log(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_repo(tmp_path, action_log: bool | None = True) -> Path:
    """An observed checkout whose state dir is `<repo>/.autoloop`, said in the
    config the dashboard reads, with a `workers_root` so the inbox `collect()`
    globs is this test's own and not the operator's.

    `action_log` is written as `[audit] action_log = <bool>`; `None` writes no
    `[audit]` section at all, which is the shipped default and means OFF. The
    default here is ON, because most tests in this file are about a round
    that is writing a log and the loop only does that with the flag on."""
    repo = make_repo_from_template(tmp_path / "repo", branch="work")
    (repo / ".autoloop").mkdir()
    audit = "" if action_log is None else f"\n[audit]\naction_log = {json.dumps(action_log)}\n"
    (repo / ".autoloop" / "config.toml").write_text(
        '[paths]\nstate_dir = ".autoloop"\n'
        f"workers_root = {json.dumps(str(tmp_path / 'workers'))}\n" + audit,
        encoding="utf-8",
    )
    return repo


def bytes_read_from(monkeypatch, path: Path) -> list[int]:
    """Every `os.read` return on a descriptor the reader opened for `path`,
    by length — what was actually READ, as opposed to what `_tail_bytes`
    returned. An implementation that read the file whole and sliced the tail
    returns the same bytes and is caught only here. Descriptors are matched
    by the `os.open` that produced them, so nothing else a test worker reads
    during the call is counted."""
    ours: set[int] = set()
    sizes: list[int] = []
    real_open, real_read, real_close = os.open, os.read, os.close

    def spy_open(file, flags, *args, **kwargs):
        fd = real_open(file, flags, *args, **kwargs)
        if os.fspath(file) == str(path):
            ours.add(fd)
        return fd

    def spy_read(fd, size):
        data = real_read(fd, size)
        if fd in ours:
            sizes.append(len(data))
        return data

    def spy_close(fd):
        ours.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "read", spy_read)
    monkeypatch.setattr(os, "close", spy_close)
    return sizes


def panel_js() -> str:
    """The panel's own render, lifted verbatim out of the served page. The
    region declares nothing but `renderActionLog` and reaches nothing else on
    the page — no `esc`, no `rows` — which is what lets it run against a stub
    document instead of a browser."""
    script = PAGE.split("<script>", 1)[1]
    return script.split("// ACTION_LOG_START", 1)[1].split("// ACTION_LOG_END", 1)[0]


#: A stub document whose three nodes RECORD every `innerHTML` write instead of
#: performing one. That is the mutation this file is against: a render that
#: assigned `innerHTML` — raw, or with `esc()` in front of it — leaves
#: `textContent` untouched and lands in `WRITES`, and both are asserted on.
STUB_DOCUMENT = """
const WRITES = [];
function node(id){
  const n = {id: id, textContent: "", style: {display: ""},
             scrollTop: 0, scrollHeight: 0, clientHeight: 0};
  Object.defineProperty(n, "innerHTML", {
    get(){ return ""; },
    set(v){ WRITES.push([id, String(v)]); },
  });
  return n;
}
const NODES = {};
for (const id of ["actionlogbox", "actionlognote", "actionlog"]) NODES[id] = node(id);
const document = {getElementById: id => NODES[id]};
const snapshot = () => ({
  display: NODES.actionlogbox.style.display,
  preDisplay: NODES.actionlog.style.display,
  note: NODES.actionlognote.textContent,
  tail: NODES.actionlog.textContent,
  writes: WRITES.slice(),
});
"""


def run_js(source: str) -> str:
    """Run `source` under node and return its stdout. A local copy of
    `test_dashboard.py`'s helper: these modules are not a package. Skipped
    rather than faked when node is absent — a hand-rolled JS interpreter would
    be testing the interpreter."""
    import shutil
    import tempfile

    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment without node
        pytest.skip("node is required to run the page's own helpers")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(source)
        path = handle.name
    result = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"the page's helpers threw:\n{result.stderr[:800]}"
    return result.stdout


def render(*payloads) -> list[dict]:
    """Run `renderActionLog` once per payload, in order, against one stub
    document, and return the document's state after each — so a test can
    assert what the SECOND render did to what the first one left behind."""
    calls = "".join(
        f"renderActionLog({json.dumps(p)}); OUT.push(snapshot());\n" for p in payloads
    )
    harness = panel_js() + STUB_DOCUMENT + "const OUT = [];\n" + calls \
        + "console.log(JSON.stringify(OUT));\n"
    return json.loads(run_js(harness))


# ---- a running round renders the tail of its log -----------------------------


def test_a_running_round_renders_the_tail_of_its_log(tmp_path):
    """Through `collect()`, because the wiring is the claim: the payload carries
    the tail under `action_log`, read off the SAME state dir and the SAME
    `task_execution` every other panel reads, and the page renders it."""
    repo = make_repo(tmp_path)
    (repo / ".autoloop" / "state.json").write_text(json.dumps(running()), encoding="utf-8")
    log = write_log(convention_path(repo / ".autoloop"),
                    "Read autoloop/dashboard.py\nGrep 'action_log'\nEdit autoloop/dashboard.py\n")

    payload = collect(repo)

    view = payload["action_log"]
    assert view["state"] == "lines"
    assert view["task_id"] == TASK
    assert view["path"] == str(log)
    assert view["lines"] == ["Read autoloop/dashboard.py", "Grep 'action_log'",
                             "Edit autoloop/dashboard.py"]
    assert view["tail"] == "\n".join(view["lines"])
    assert view["truncated"] is False
    assert str(log) in view["note"], "the note names the file the tail came from"

    # …and the page shows it: the box is visible and the text is the tail.
    [shown] = render(view)
    assert shown["display"] == "" and shown["preDisplay"] == ""
    assert shown["tail"] == view["tail"]
    assert shown["note"] == view["note"]

    # The round appends and the next polls carry a longer tail. The render
    # skips the DOM write while the tail is unchanged (so a poll cannot reset
    # the operator's scroll or selection) — and that guard must still let a
    # CHANGED tail through, or the panel freezes on the first tail it saw while
    # looking live, which is the failure this panel exists to prevent.
    grown = {**view, "tail": view["tail"] + "\nBash: pytest -q", "lines": view["lines"] + ["Bash: pytest -q"]}
    same, same_again, then_grown = render(view, view, grown)
    assert same["tail"] == same_again["tail"] == view["tail"]
    assert then_grown["tail"] == grown["tail"]
    assert then_grown["writes"] == []


def test_the_execution_record_names_the_log_and_the_convention_is_only_a_fallback(tmp_path):
    """The WRITER stays the authority on where it writes. A record carrying
    `action_log_path` is read at that path even when the convention path also
    exists — a reader that insisted on its own convention would go on saying
    "nothing written yet" beside a busy round the day the file moved."""
    named = write_log(tmp_path / "elsewhere" / "round.log", "named: Write foo.py\n")
    write_log(convention_path(tmp_path / "state"), "convention: Write bar.py\n")

    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(named)}), tmp_path / "state")

    assert view["state"] == "lines"
    assert view["lines"] == ["named: Write foo.py"]
    assert view["path"] == str(named)
    # And without the field, the convention path — named in the payload so a
    # mismatch with the eventual writer is a visible path, not a silent idle.
    view = action_log_tail(running(), tmp_path / "state")
    assert view["lines"] == ["convention: Write bar.py"]
    assert view["path"] == str(convention_path(tmp_path / "state"))


# ---- discovery: the file the WRITER names, newest round first --------------------


def test_discovery_finds_the_file_the_real_writer_opens(tmp_path):
    """Pinned against the writer itself, not a name this file composed: a
    `ClaudeCliRunner` given `<state_dir>/action-logs` — what `cli._build_executor`
    passes it — opens this round's log exactly as a live round does, writes
    one line through it, and the page shows THAT file's tail through
    `collect()`. This is the test that fails the day the writer's naming
    moves; a fixture shaped like the name could not. It also refuses the
    round-1 guess by name, so the regression cannot come back as "a file was
    found" that is the wrong file."""
    repo = make_repo(tmp_path)
    # Dispatched by the real clock, since the writer stamps by it: the record
    # is written before the runner is built, so its stamp is the later one.
    (repo / ".autoloop" / "state.json").write_text(
        json.dumps(running(started_at=dispatched_just_now())), encoding="utf-8")
    runner = ClaudeCliRunner(repo_root=repo, action_log_dir=convention_dir(repo / ".autoloop"))
    log = runner._open_action_log(AgentSpec(domain=TASK, title="dash-08", prompt="p"))
    assert log.active and log.path is not None, log.problem
    log.write("stderr", "Read autoloop/dashboard.py\n")
    log.close()

    view = collect(repo)["action_log"]

    assert view["state"] == "lines", view["note"]
    assert view["path"] == str(log.path)
    guess = convention_dir(repo / ".autoloop") / f"{TASK}{ACTION_LOG_SUFFIX}"
    assert view["path"] != str(guess), "round 1's guess must not be what was found"
    assert view["lines"][0].startswith("# autoloop action log"), "the writer's own header leads"
    assert view["lines"][-1] == "Read autoloop/dashboard.py"
    assert view["truncated"] is False


def test_the_directory_looked_in_is_the_one_config_resolves(tmp_path):
    """`ACTION_LOG_DIRNAME` restates `config.action_log_dir` — restated because
    that property needs a loaded `AutoloopConfig` and this page resolves the
    state dir on its own — and a restatement is safe only while it is pinned:
    the day the writer's directory moves, this is the line that says so."""
    config = AutoloopConfig(
        browser=BrowserConfig(), policy=PolicyConfig(), state_dir=tmp_path / "state"
    )
    assert config.action_log_dir == convention_dir(config.state_dir)


def test_the_newest_round_log_is_shown_and_only_this_tasks(tmp_path):
    """A round that re-ran its agent (the advisory rendezvous) appends to one
    file, but a round that was RE-DISPATCHED after a crash inside the same
    second would leave two — the page shows the NEWEST by the writer's stamp,
    which is UTC and fixed-width so the greatest name is the latest. And the
    match is the writer's whole shape: a task whose slug is a PREFIX of
    another's (`dash` beside `dash-08`) sees only its own, and neither round
    1's `<task_id>.log` nor a stray `.txt` is a candidate."""
    state_dir = tmp_path / "state"
    write_log(convention_path(state_dir, stamp="20260911T100000.000000-1-0"), "older this second\n")
    write_log(convention_path(state_dir, stamp="20260911T100000.500000-1-1"), "newest round\n")
    write_log(convention_path(state_dir, task_id="dash", stamp="20260911T110000.000000-1-2"),
              "another task, later\n")
    write_log(convention_dir(state_dir) / f"{TASK}{ACTION_LOG_SUFFIX}", "round-1 guess\n")
    write_log(convention_dir(state_dir) / f"{TASK}-{STAMP}.txt", "not a log\n")

    view = action_log_tail(running(), state_dir)
    assert view["state"] == "lines"
    assert view["lines"] == ["newest round"]
    assert view["path"] == str(convention_path(state_dir, stamp="20260911T100000.500000-1-1"))

    other = action_log_tail(running("dash"), state_dir)
    assert other["lines"] == ["another task, later"]
    assert other["path"] == str(convention_path(state_dir, task_id="dash",
                                                stamp="20260911T110000.000000-1-2"))
    # A task id the slug rule has to clean still finds its own file, under the
    # cleaned name the writer would have used.
    write_log(convention_path(state_dir, task_id="x/y z"), "cleaned\n")
    assert action_log_tail(running("x/y z"), state_dir)["lines"] == ["cleaned"]


def test_an_earlier_rounds_log_is_named_and_not_shown_as_this_rounds(tmp_path):
    """The stale tail with a round RUNNING. A revision is dispatched with the
    previous round's file already on disk, and until this round's runner opens
    its own — or for the whole round, if the writer was on then and is off
    now — "newest file wins" rendered that trace under "this round's log".
    The reader compares the writer's stamp against `current_task.started_at`:
    a file stamped before this dispatch is an earlier round's, so the state is
    `missing`, the file is NAMED (an operator can still go and read it) and
    nothing of it reaches `lines` or `tail`. The moment this round's own file
    appears it wins, whatever else is there."""
    state_dir = tmp_path / "state"
    earlier = write_log(convention_path(state_dir, stamp=EARLIER_STAMP),
                        "round 1: Edit foo.py\n")

    view = action_log_tail(running(), state_dir, enabled=True)

    assert view["state"] == "missing", view["note"]
    assert view["note"].startswith(ACTION_LOG_MISSING + looked_for(state_dir))
    assert view["note"] == ACTION_LOG_MISSING + looked_for(state_dir) \
        + ACTION_LOG_OLDER_ROUND.format(path=earlier, since="20260911T100000") \
        + ACTION_LOG_SETTING_ON
    assert view["lines"] == [] and view["tail"] == "" and view["path"] == looked_for(state_dir)
    assert "round 1" not in view["tail"]
    [shown] = render(view)
    assert shown["preDisplay"] == "none" and shown["tail"] == ""

    # This round opens its file: shown, and the earlier one is not mentioned.
    current = write_log(convention_path(state_dir), "round 2: Read bar.py\n")
    view = action_log_tail(running(), state_dir, enabled=True)
    assert view["state"] == "lines" and view["path"] == str(current)
    assert view["lines"] == ["round 2: Read bar.py"]
    assert str(earlier) not in view["note"]

    # Through `collect()` too, since `current_task` is read off the same state
    # file as the record: the page reports the earlier round's file, not its tail.
    repo = make_repo(tmp_path)
    write_log(convention_path(repo / ".autoloop", stamp=EARLIER_STAMP), "round 1: Edit foo.py\n")
    (repo / ".autoloop" / "state.json").write_text(json.dumps(running()), encoding="utf-8")
    view = collect(repo)["action_log"]
    assert view["state"] == "missing" and view["tail"] == ""
    assert str(convention_path(repo / ".autoloop", stamp=EARLIER_STAMP)) in view["note"]


def test_no_parseable_dispatch_stamp_means_no_filter_never_a_hidden_log(tmp_path):
    """The fail direction, stated: hiding a LIVE round's log because a stamp
    would not parse is worse than showing an old one, and the shown log's
    sentence names the file. So `current_task` naming a different task, or
    carrying a `started_at` that is missing, blank or not a date, filters
    nothing — the newest file wins, as before round 2."""
    state_dir = tmp_path / "state"
    earlier = write_log(convention_path(state_dir, stamp=EARLIER_STAMP), "round 1: Edit foo.py\n")

    for state in (
        running(started_at=""),
        running(started_at="not a date"),
        running(started_at=None),
        {**running(), "current_task": {"task_id": "someone-else", "started_at": DISPATCHED_AT}},
        {**running(), "current_task": None},
    ):
        view = action_log_tail(state, state_dir, enabled=True)
        assert view["state"] == "lines", (state["current_task"], view["note"])
        assert view["path"] == str(earlier)
        assert view["lines"] == ["round 1: Edit foo.py"]

    # And a naive stamp is read as UTC, exactly as `_elapsed_seconds` reads it,
    # so a record written before stamps were tz-aware still filters correctly.
    view = action_log_tail(running(started_at="2026-09-11T10:00:00"), state_dir, enabled=True)
    assert view["state"] == "missing" and str(earlier) in view["note"]
    # A stamp given in another zone is compared in UTC: 12:00 at +02:00 is
    # 10:00 UTC, the fixture's dispatch second, and the earlier file predates it.
    view = action_log_tail(running(started_at="2026-09-11T12:00:00+02:00"), state_dir, enabled=True)
    assert view["state"] == "missing" and str(earlier) in view["note"]
    write_log(convention_path(state_dir), "round 2\n")
    view = action_log_tail(running(started_at="2026-09-11T12:00:00+02:00"), state_dir, enabled=True)
    assert view["lines"] == ["round 2"]


def test_a_log_directory_that_cannot_be_listed_is_unreadable_not_missing(tmp_path):
    """The listing has its own failure and it is not "nothing written yet":
    an `action-logs` that exists and cannot be read as a directory — a regular
    file in its place, portably — names the directory and the OS's reason."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    convention_dir(state_dir).write_text("not a directory\n", encoding="utf-8")

    view = action_log_tail(running(), state_dir)

    assert view["state"] == "unreadable"
    assert view["note"].startswith(ACTION_LOG_UNREADABLE)
    assert str(convention_dir(state_dir)) in view["note"]
    assert not view["note"].startswith(ACTION_LOG_MISSING)
    assert view["path"] == looked_for(state_dir)


# ---- the content is text, never markup ----------------------------------------


#: What an agent might print, chosen so that every route into the DOM other than
#: `textContent` leaves a visible trace: a script element, an event handler in
#: an attribute, a bare ampersand, a template-literal placeholder, a backtick,
#: and both kinds of quote.
HOSTILE_TAIL = (
    'Bash: echo "<script>alert(1)</script>" > out.html\n'
    "Write index.html: <img src=x onerror='steal()'> & ${document.cookie} `tick`\n"
    'Read "quoted" \'file\' </pre><b>bold</b>'
)


def test_arbitrary_log_content_reaches_the_page_as_text_never_as_markup(tmp_path):
    """The one place on the page where genuinely unpredictable content arrives.
    Asserted by RUNNING the render, not by reading its source: the text on the
    node must be byte-identical to what was in the file, and no `innerHTML`
    write may have happened at all — raw interpolation fails the first, and
    `innerHTML = esc(tail)` fails both (the stub records the write and never
    sets `textContent`)."""
    log = write_log(tmp_path / "hostile.log", HOSTILE_TAIL + "\n")

    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))

    # The backend hands the page the RAW text. Escaping here as well would show
    # `&lt;` for every `<` the agent printed once the page escapes it again.
    assert view["tail"] == HOSTILE_TAIL
    assert "&lt;" not in view["tail"] and "&amp;" not in view["tail"]

    [shown] = render(view)
    assert shown["tail"] == HOSTILE_TAIL, "the DOM text must be the file's text, character for character"
    assert shown["writes"] == [], f"the log must never reach the DOM as HTML: {shown['writes']}"


def test_the_note_is_text_too_because_a_path_is_not_ours_to_trust(tmp_path):
    """The sentence beside the box carries a path an operator configured and,
    on the unreadable branch, an OS error message. Neither is under this
    module's control, so both take the same route as the tail."""
    hostile_dir = tmp_path / "<state>&'dir'"
    view = action_log_tail(running(), hostile_dir)  # missing: the note names the path

    assert view["state"] == "missing"
    assert str(hostile_dir) in view["note"]
    [shown] = render(view)
    assert shown["note"] == view["note"]
    assert shown["writes"] == []


# ---- no round, no panel ---------------------------------------------------------


def test_no_running_round_renders_no_panel_rather_than_a_stale_tail(tmp_path):
    """`state.task_execution` is cleared the moment a candidate is published,
    so its absence is the loop's own statement that nothing is in flight. The
    last round's log is still on disk and `current_task` still names it —
    neither may resurrect a tail beside an idle loop."""
    repo = make_repo(tmp_path)
    write_log(convention_path(repo / ".autoloop"), "last round: Edit foo.py\n")
    (repo / ".autoloop" / "state.json").write_text(json.dumps({
        "phase": "ready",
        "current_task": {"task_id": TASK, "started_at": "2026-09-11T10:00:00+00:00"},
        "task_execution": None,
    }), encoding="utf-8")

    assert collect(repo)["action_log"] is None
    # The same answer for a record that names no task at all.
    assert action_log_tail({"task_execution": {"task_id": ""}}, repo / ".autoloop") is None
    assert action_log_tail({}, repo / ".autoloop") is None

    # And the page: a round renders, then the next poll carries no round. The
    # box hides AND both nodes are cleared — a hidden node still holding the
    # last round's text is one CSS rule from showing it again.
    live = action_log_tail(running(), repo / ".autoloop")
    assert live["state"] == "lines"
    shown, then_idle = render(live, None)
    assert shown["tail"] == "last round: Edit foo.py"
    assert then_idle["display"] == "none"
    assert then_idle["tail"] == "" and then_idle["note"] == ""
    # The section ships hidden, so the first paint before any poll shows nothing.
    assert '<section id="actionlogbox" style="display:none">' in PAGE


# ---- the absences are different sentences ------------------------------------


def test_a_missing_log_and_an_unreadable_log_render_differently(tmp_path):
    """Nothing-written-yet and cannot-read-the-log call for opposite
    reactions — wait, or go and look — and an empty box supports neither.
    The two states carry different words, both name the path, and the page
    shows the sentence with NO box under it, because an empty box under
    "could not be read" reads as "the log is empty", a third state again."""
    state_dir = tmp_path / "state"
    path = convention_path(state_dir)

    # `enabled=True`: the writer is on, so an empty directory is the round not
    # having written yet. (`enabled=False` is `off`, its own test below.)
    missing = action_log_tail(running(), state_dir, enabled=True)
    assert missing["state"] == "missing"
    # Names what was LOOKED FOR — the writer's directory and the task's glob —
    # since there is no file to name; that is what makes a naming mismatch a
    # visible path rather than a permanently idle agent. And says that the
    # setting is read at start: "on in the file" is not "on in the loop".
    assert missing["note"] == ACTION_LOG_MISSING + looked_for(state_dir) + ACTION_LOG_SETTING_ON
    assert "reads it once at start" in ACTION_LOG_SETTING_ON
    assert missing["path"] == looked_for(state_dir)
    assert missing["lines"] == [] and missing["tail"] == ""

    # Unreadable, portably: something IS there under the round log's own name
    # and it is not a file the page may read — a directory here, found by the
    # listing and refused by `fstat`; a fifo, which could block the poll, has
    # its own tests further down.
    path.mkdir(parents=True)
    unreadable = action_log_tail(running(), state_dir, enabled=True)
    assert unreadable["state"] == "unreadable"
    assert unreadable["note"].startswith(ACTION_LOG_UNREADABLE)
    assert str(path) in unreadable["note"]
    assert "not a regular file" in unreadable["note"]

    assert missing["state"] != unreadable["state"]
    assert missing["note"] != unreadable["note"]
    for view in (missing, unreadable):
        [shown] = render(view)
        assert shown["display"] == "", "the panel shows: the round IS running"
        assert shown["note"] == view["note"]
        assert shown["preDisplay"] == "none" and shown["tail"] == "", "no empty box"
        assert shown["writes"] == []


def test_the_default_config_reads_off_rather_than_pending(tmp_path):
    """The most ordinary deployment there is: `[audit] action_log` unset, which
    is `false`, and a round running. Before round 2 the page said "Nothing
    written yet" beside it — the sentence that means "wait" — for a file no
    round would ever write. Through `collect()`, off the same config it reads
    everything else from: no `[audit]` at all and an explicit `false` are both
    `off`, the sentence names the key and the file so the remedy is in it, it
    says the loop reads the setting at START, it does not say "yet", and the
    page shows it as a sentence with no box."""
    for setting in (None, False):
        repo = make_repo(tmp_path / f"cfg-{setting}", action_log=setting)
        (repo / ".autoloop" / "state.json").write_text(json.dumps(running()), encoding="utf-8")

        view = collect(repo)["action_log"]

        assert view["state"] == "off", (setting, view["note"])
        config = repo / ".autoloop" / "config.toml"
        assert view["note"] == ACTION_LOG_OFF.format(
            setting=ACTION_LOG_SETTING, config=config,
            looked_for=looked_for(repo / ".autoloop"))
        assert ACTION_LOG_SETTING in view["note"] and str(config) in view["note"]
        assert "restart" in view["note"] and "at start" in view["note"]
        assert not view["note"].startswith(ACTION_LOG_MISSING)
        assert "yet" not in view["note"]
        assert view["path"] == looked_for(repo / ".autoloop")
        assert view["lines"] == [] and view["tail"] == ""
        [shown] = render(view)
        assert shown["display"] == "" and shown["preDisplay"] == "none"
        assert shown["note"] == view["note"] and shown["writes"] == []

    # With the writer ON and nothing there, the calm sentence is the right one
    # — with the caveat that the loop reads the key at start, since "true in
    # the file" is not "true in the running loop".
    repo = make_repo(tmp_path / "on", action_log=True)
    (repo / ".autoloop" / "state.json").write_text(json.dumps(running()), encoding="utf-8")
    view = collect(repo)["action_log"]
    assert view["state"] == "missing"
    assert view["note"] == ACTION_LOG_MISSING + looked_for(repo / ".autoloop") + ACTION_LOG_SETTING_ON


def test_a_log_that_is_there_is_shown_whatever_the_setting_says_now(tmp_path):
    """`off` is decided only after discovery came back empty. The loop reads
    the setting once at start, so a file on disk beside a config that now says
    `false` is a round that IS writing — the file is the fact, and hiding it
    behind "the log is off" would be the lie in the other direction."""
    repo = make_repo(tmp_path, action_log=False)
    (repo / ".autoloop" / "state.json").write_text(json.dumps(running()), encoding="utf-8")
    log = write_log(convention_path(repo / ".autoloop"), "Edit foo.py\n")

    view = collect(repo)["action_log"]

    assert view["state"] == "lines" and view["path"] == str(log)
    assert view["lines"] == ["Edit foo.py"]
    # And the same for a directly-named `enabled=False`, plus the older-round
    # clause when the only file is an earlier round's: off, naming that file.
    state_dir = tmp_path / "state"
    write_log(convention_path(state_dir), "Edit foo.py\n")
    assert action_log_tail(running(), state_dir, enabled=False)["state"] == "lines"
    earlier = write_log(convention_path(tmp_path / "old", stamp=EARLIER_STAMP), "round 1\n")
    view = action_log_tail(running(), tmp_path / "old", enabled=False, config_path="cfg.toml")
    assert view["state"] == "off"
    assert view["note"].endswith(ACTION_LOG_OLDER_ROUND.format(path=earlier, since="20260911T100000"))
    assert "cfg.toml" in view["note"]


def test_a_setting_that_was_not_established_claims_neither_off_nor_wait(tmp_path):
    """A caller that did not read the config — `enabled=None`, the default for
    a direct call — gets `missing` with a clause saying the setting was not
    checked and that its default is off. The panel must not claim "off" from
    a file it never saw, and must not imply "wait" without a caveat either.
    `collect()` reaches `None` only when the config could not be parsed, and
    then no state directory resolves and no panel renders at all — so this is
    pinned on `_action_log_setting` directly: an unparseable file is `None`,
    an empty one and a missing key are `False`, and `true` is `True`."""
    state_dir = tmp_path / "state"
    view = action_log_tail(running(), state_dir)
    assert view["state"] == "missing"
    assert view["note"] == ACTION_LOG_MISSING + looked_for(state_dir) + ACTION_LOG_SETTING_UNCHECKED
    assert "false by default" in view["note"]

    repo = tmp_path / "repo"
    config = repo / ".autoloop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[audit\nnot toml", encoding="utf-8")
    assert dashboard._action_log_setting(repo) == (None, config)
    config.write_text("", encoding="utf-8")
    assert dashboard._action_log_setting(repo) == (False, config)
    config.write_text("[audit]\nother = 1\n", encoding="utf-8")
    assert dashboard._action_log_setting(repo) == (False, config)
    config.write_text('[audit]\naction_log = "true"\n', encoding="utf-8")
    assert dashboard._action_log_setting(repo) == (False, config), "a string is not `true`"
    config.write_text("[audit]\naction_log = true\n", encoding="utf-8")
    assert dashboard._action_log_setting(repo) == (True, config)
    config.unlink()
    assert dashboard._action_log_setting(repo) == (None, config)


def test_a_log_that_exists_but_cannot_be_opened_says_so_rather_than_nothing_written(tmp_path):
    """The fail-open this panel is about. `Path.is_file()` swallows `OSError`
    and answers `False`, so a permission fault would render as "nothing written
    yet" — the calm sentence, for the state that needs a look. A mode-0 file
    exists and the OPEN raises; the sentence carries the OS's reason rather
    than a paraphrase of it."""
    if os.geteuid() == 0:  # pragma: no cover - root reads everything
        pytest.skip("root is not refused by mode bits")
    log = write_log(tmp_path / "state" / "locked.log", "Edit foo.py\n")
    log.chmod(0)
    try:
        view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))
    finally:
        log.chmod(0o600)

    assert view["state"] == "unreadable"
    assert view["note"].startswith(ACTION_LOG_UNREADABLE)
    assert str(log) in view["note"]
    assert "Permission denied" in view["note"], "the OS's own reason, not a paraphrase"
    assert not view["note"].startswith(ACTION_LOG_MISSING)


def test_an_empty_log_is_a_third_sentence_not_a_missing_one(tmp_path):
    """The round opened the file and has logged nothing into it yet — a
    different fact from no file at all, and rendered as one."""
    log = write_log(tmp_path / "empty.log", "")

    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))

    assert view["state"] == "empty"
    assert view["note"] == ACTION_LOG_EMPTY + str(log)
    [shown] = render(view)
    assert shown["preDisplay"] == "none" and shown["note"] == view["note"]


def test_a_log_smaller_than_the_byte_budget_is_read_whole_never_reported_empty(tmp_path):
    """The bug the first run of this file caught, pinned on its own so it cannot
    come back as "a small log is empty". `_tail_bytes` measures the size by
    seeking to the END, and it used to seek back only when there was something
    to skip — so every log under `ACTION_LOG_TAIL_BYTES` was read from EOF, and
    a busy round's forty-line log rendered as "has logged nothing into it yet",
    the calm sentence for the one state that is not calm. One byte, one byte
    under the budget and exactly the budget are all `lines`, whole and not
    truncated."""
    for size in (1, ACTION_LOG_TAIL_BYTES - 1, ACTION_LOG_TAIL_BYTES):
        log = tmp_path / f"{size}.log"
        log.write_bytes(b"x" * (size - 1) + b"\n" if size > 1 else b"x")

        view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))

        assert view["state"] == "lines", (size, view["state"], view["note"])
        assert view["truncated"] is False, size
        assert view["lines"] == ["x" * max(1, size - 1)], size


def test_every_action_log_state_is_reachable_and_the_vocabulary_is_closed(tmp_path):
    """The six states, each produced, so the vocabulary the page keys off
    cannot drift from what the backend emits. `unlocatable` is the one
    `collect()` cannot reach — an unresolvable state dir reads `state.json` as
    `{}`, so there is no task to look for — and it exists for a caller that
    hands a running record and no directory: the honest answer is "nowhere to
    look", not the convention path resolved against nothing."""
    state_dir = tmp_path / "state"
    produced = {
        action_log_tail(running(), None)["state"],
        action_log_tail(running(), state_dir)["state"],
        action_log_tail(running(), state_dir, enabled=False)["state"],
    }
    log = write_log(convention_path(state_dir), "")
    produced.add(action_log_tail(running(), state_dir)["state"])
    log.write_text("Read a.py\n", encoding="utf-8")
    produced.add(action_log_tail(running(), state_dir)["state"])
    log.unlink()
    log.mkdir()
    produced.add(action_log_tail(running(), state_dir)["state"])

    assert produced == set(ACTION_LOG_STATES)
    assert action_log_tail(running(), None)["note"] == ACTION_LOG_UNLOCATABLE
    assert ACTION_LOG_PATH_FIELD in ACTION_LOG_UNLOCATABLE, "the sentence names the field that would fix it"


# ---- only the tail is read ------------------------------------------------------


def numbered(count: int) -> list[str]:
    return [f"line-{n:07d} Bash: run step {n}" for n in range(count)]


def test_only_the_tail_is_read_never_the_whole_file(tmp_path, monkeypatch):
    """A 25-minute round produces a large log and the page polls every two
    seconds. Measured on the bytes the reader actually READS — every `os.read`
    on the descriptor it opened for the file, summed — against a log far
    larger than the budget: the read is bounded by `ACTION_LOG_TAIL_BYTES`
    whatever the file's size, the shown lines are the LAST ones, whole, and
    the panel says earlier lines exist. Round 1 measured what `_tail_bytes`
    RETURNED, which "read the whole file and slice the tail" satisfies; that
    mutation reads 5 MB here and fails."""
    lines = numbered(150_000)  # ~5 MB
    log = write_log(tmp_path / "big.log", "\n".join(lines) + "\n")
    assert log.stat().st_size > 40 * ACTION_LOG_TAIL_BYTES

    read = bytes_read_from(monkeypatch, log)
    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))

    assert read, "the reader never read the file through os.read, so nothing was measured"
    assert 0 < sum(read) <= ACTION_LOG_TAIL_BYTES, sum(read)
    assert view["state"] == "lines"
    assert view["truncated"] is True
    assert "Earlier lines are NOT shown" in view["note"]
    assert 0 < len(view["lines"]) <= ACTION_LOG_TAIL_LINES
    assert view["lines"] == lines[-len(view["lines"]):], "the tail is the END of the file"
    assert lines[0] not in view["tail"]
    # Every shown line is a line the file holds — never the fragment left where
    # the byte window cut a line in half.
    whole = set(lines)
    assert all(line in whole for line in view["lines"])


def test_a_log_with_no_newline_at_all_is_bounded_the_same_way(tmp_path, monkeypatch):
    """The case a line-count bound alone would miss: one enormous line. The
    byte budget is the real bound, so the reader costs the same here as on a
    forty-line log, and what it shows is the END of that line, marked
    truncated, rather than a blank panel."""
    log = tmp_path / "oneline.log"
    log.write_bytes(b"x" * (30 * ACTION_LOG_TAIL_BYTES) + b" ...the end")

    read = bytes_read_from(monkeypatch, log)
    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))

    assert read and 0 < sum(read) <= ACTION_LOG_TAIL_BYTES, sum(read)
    assert view["state"] == "lines"
    assert len(view["lines"]) == 1
    assert view["lines"][0].endswith(" ...the end")
    assert len(view["lines"][0]) <= ACTION_LOG_TAIL_BYTES
    assert view["truncated"] is True


def test_the_line_bound_keeps_the_newest_lines_and_the_first_cut_line_is_dropped(tmp_path):
    """Inside the byte window, only the last `max_lines` render, newest last;
    and after a mid-file seek the first line in the window is the END of a line
    whose start was skipped — dropped, because a fragment shown as a line reads
    as a command that was never run."""
    lines = numbered(30)  # under `ACTION_LOG_TAIL_LINES`, so the default shows them all
    assert len(lines) < ACTION_LOG_TAIL_LINES
    log = write_log(tmp_path / "log", "\n".join(lines) + "\n")

    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}), max_lines=5)
    assert view["lines"] == lines[-5:]
    assert view["truncated"] is True and view["max_lines"] == 5

    # A byte budget that lands mid-line: the fragment must not be a line.
    cut = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}), max_bytes=100)
    assert cut["truncated"] is True
    assert all(line in set(lines) for line in cut["lines"]), cut["lines"]
    assert cut["lines"][-1] == lines[-1]

    # Nothing to show and nothing skipped: the small log is not "truncated".
    small = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))
    assert small["lines"] == lines and small["truncated"] is False


def test_bytes_that_are_not_utf8_do_not_blank_the_panel(tmp_path):
    """Arbitrary bytes an agent printed, and a window that starts mid-character
    by construction: decoded with replacement, never strictly, so one invalid
    sequence cannot take the whole tail with it."""
    log = tmp_path / "bytes.log"
    log.write_bytes(b"Bash: cat caf\xc3\xa9.txt\n\xff\xfe broken \xc3\nRead ok.py\n")

    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))

    assert view["state"] == "lines"
    assert view["lines"][0] == "Bash: cat café.txt"
    assert view["lines"][-1] == "Read ok.py"
    assert "�" in view["lines"][1]


# ---- the open cannot block: a fifo at the path, there before or put there at the open


#: `os.open` as it was before any test in this process patched it. The watchdog
#: below rescues a reader through it, so a test that patches `os.open` cannot
#: route its own rescue back into the patch.
_REAL_OPEN = os.open

#: How long the watchdog waits before releasing a reader that BLOCKED. The
#: passing case returns in microseconds and never meets it; a regression pays
#: it once and then FAILS on `fired` — instead of wedging the whole run, which
#: has no pytest-timeout and sits under `-n auto` where a stuck worker is a
#: stuck suite.
WATCHDOG_SECONDS = 5.0


class Watchdog:
    """Releases a reader blocked in `open()` on the fifo at `path`, by opening
    its WRITER end after `WATCHDOG_SECONDS`, and records that it had to.

    `O_WRONLY | O_NONBLOCK` succeeds precisely when a reader is there — on
    Linux and macOS a reader blocked in `open()` has already counted itself —
    and fails `ENXIO` when none is, which is swallowed: in the passing case the
    timer is cancelled before it fires, and `fired` stays empty. `fired` is the
    assertion; the state the reader then reports is only the second half."""

    def __init__(self, path: Path):
        self.path = path
        self.fired: list[float] = []
        self._timer = threading.Timer(WATCHDOG_SECONDS, self._release)
        self._timer.daemon = True

    def _release(self):
        self.fired.append(time.monotonic())
        try:
            fd = _REAL_OPEN(self.path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        os.close(fd)

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()


def test_a_fifo_at_the_path_is_refused_without_blocking_the_poll(tmp_path):
    """`action_log_path` comes off an operator-editable record, so a fifo at
    the path needs no race to be reachable. Opened NON-BLOCKING it hands back
    a descriptor at once, `fstat` says what it is, and the panel says "not a
    regular file" — with no writer ever appearing. This is what pins
    `O_NONBLOCK` empirically: an open without it waits for a writer that never
    comes, and only the watchdog would let that FAIL rather than hang."""
    if not hasattr(os, "mkfifo"):  # pragma: no cover - no fifos on this platform
        pytest.skip("this platform has no fifos")
    fifo = tmp_path / "state" / "fifo.log"
    fifo.parent.mkdir()
    os.mkfifo(fifo)

    with Watchdog(fifo) as watchdog:
        started = time.monotonic()
        view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(fifo)}))
        elapsed = time.monotonic() - started

    assert not watchdog.fired, f"the open BLOCKED for {elapsed:.1f}s until the watchdog released it"
    assert view["state"] == "unreadable", view
    assert view["note"].startswith(ACTION_LOG_UNREADABLE)
    assert str(fifo) in view["note"]
    assert "not a regular file" in view["note"]
    assert view["lines"] == [] and view["tail"] == ""


def test_a_log_replaced_by_a_fifo_at_the_moment_it_is_opened_cannot_block_the_poll(tmp_path, monkeypatch):
    """The race the review of round 1 named. Round 1 asked `os.stat` whether
    the path held a regular file and then opened the PATH again: two lookups,
    and a fifo put there between them met a blocking open that waited for a
    writer forever — on every poll, from every scheduler. Reproduced
    deterministically: the log is a regular file until the reader's own
    `os.open` is entered and becomes a fifo INSIDE that call, which is the
    window no check made before the open can see. The reader must come back at
    once with `unreadable` — what the DESCRIPTOR turned out to be — never
    `lines` from a stat about a file that is no longer there, and never a hang.

    Against the round-1 code this fails in the right direction without
    hanging: `Path.open` does not pass through `os.open`, so the swap never
    fires and the first assertion says so. The descriptor is then checked to
    be closed — a leak per poll, every two seconds, exhausts the table within
    the hour — and to have been the ONLY open of the path: a second open is a
    second lookup, and the race is back."""
    if not hasattr(os, "mkfifo"):  # pragma: no cover - no fifos on this platform
        pytest.skip("this platform has no fifos")
    log = write_log(tmp_path / "state" / "swap.log", "Edit foo.py\nBash: pytest -q\n")
    opened: list[int] = []  # every descriptor the reader was handed for `log`

    def swap_then_open(file, flags, *args, **kwargs):
        ours = os.fspath(file) == str(log)
        if ours and not opened:
            log.unlink()
            os.mkfifo(log)
        fd = _REAL_OPEN(file, flags, *args, **kwargs)
        if ours:
            opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", swap_then_open)
    with Watchdog(log) as watchdog:
        started = time.monotonic()
        view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))
        elapsed = time.monotonic() - started

    assert opened, "the reader never opened the path through os.open, so the swap never happened and this test proved nothing"
    assert not watchdog.fired, f"the open BLOCKED for {elapsed:.1f}s until the watchdog released it"
    assert view["state"] == "unreadable", view
    assert "not a regular file" in view["note"]
    assert str(log) in view["note"]
    assert view["lines"] == [] and view["tail"] == ""
    assert len(opened) == 1, f"the path was opened {len(opened)} times; one lookup is the whole guarantee"
    with pytest.raises(OSError):
        os.fstat(opened[0])  # closed on the way out, refusal included


def test_a_fifo_under_the_round_logs_own_name_is_refused_without_blocking(tmp_path):
    """The same guarantee on the DISCOVERED path, which is what a real state
    directory produces: a fifo carrying a round log's name is what the listing
    finds, and the open must still not wait for a writer. The listing itself
    cannot tell a fifo from a file without a `stat` per entry — which is why
    the descriptor, not the listing, decides."""
    if not hasattr(os, "mkfifo"):  # pragma: no cover - no fifos on this platform
        pytest.skip("this platform has no fifos")
    state_dir = tmp_path / "state"
    fifo = convention_path(state_dir)
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)

    with Watchdog(fifo) as watchdog:
        started = time.monotonic()
        view = action_log_tail(running(), state_dir)
        elapsed = time.monotonic() - started

    assert not watchdog.fired, f"the open BLOCKED for {elapsed:.1f}s until the watchdog released it"
    assert view["state"] == "unreadable", view
    assert view["path"] == str(fifo)
    assert "not a regular file" in view["note"]


# ---- read-only and lock-free ------------------------------------------------------


def test_reading_the_log_writes_nothing_and_takes_no_lock(tmp_path):
    """The rest of the dashboard's load-bearing property, extended to the file
    a live round is appending to: a poll must leave the state directory
    byte-for-byte as it found it — no lock file, no marker, no touched mtime."""
    state_dir = tmp_path / "state"
    write_log(convention_path(state_dir), "Edit foo.py\n")
    before = {p: p.stat().st_mtime_ns for p in state_dir.rglob("*")}

    for _ in range(3):
        assert action_log_tail(running(), state_dir)["state"] == "lines"
    action_log_tail(running(), tmp_path / "never-created")

    assert {p: p.stat().st_mtime_ns for p in state_dir.rglob("*")} == before
    assert not (tmp_path / "never-created").exists(), "a missing log is not created"


# ---- the page: label and wiring -----------------------------------------------


def test_the_panel_is_labelled_as_the_action_log_it_is():
    """An ACTION log — tool calls, file reads and writes, commands run. Not the
    model's reasoning, which the headless CLI does not expose; a title
    promising introspection over a command trace would be worse than no panel."""
    section = PAGE.split('<section id="actionlogbox"', 1)[1].split("</section>", 1)[0]
    heading = section.split("<h2>", 1)[1].split("</h2>", 1)[0]
    assert heading.startswith("Action log")
    for word in ("tool calls", "commands run"):
        assert word in heading, f"the heading must say what the log holds: {word}"
    for promise in ("thinking", "thought process", "thoughts", "introspection"):
        assert promise not in heading.lower(), f"the heading must not promise {promise!r}"
    assert "Not the model's reasoning" in heading


def test_the_tail_stays_out_of_the_re_render_signature_and_renders_before_the_guard():
    """A live round appends continuously, so the tail changes on nearly every
    poll. In the signature, the unchanged-payload guard would never fire and
    the whole DOM would be rebuilt every 2s — so it is excluded and rendered
    before the guard, exactly as `progress` and `served_at` are. The `progress`
    line `test_dashboard.py` pins is kept intact; this one sits beside it."""
    script = PAGE.split("<script>", 1)[1]
    assert "const {served_at, progress, ...rest} = d;" in script
    assert "const {action_log, ...signed} = rest;" in script
    assert "JSON.stringify(signed)" in script
    body = script.split("function render(d, force){", 1)[1]
    assert body.index("renderActionLog(action_log);") < body.index("sig === LASTJSON")
    # And a payload from a server that predates this panel — no key at all —
    # hides it rather than throwing and blanking the page.
    [shown] = render(None)
    assert shown["display"] == "none"
