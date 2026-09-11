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

THE WRITER IS NOT IN THIS TREE. The task names stream-01 as the round that
writes the per-round file; nothing in this checkout writes one, and no record
field or document names where it will go. `action_log_tail` therefore reads the
path the execution record names (`ACTION_LOG_PATH_FIELD`) and otherwise looks
at `<state_dir>/action-logs/<task_id>.log` — and until a writer lands, a live
round's steady state on this panel is `missing`, with that path in the sentence.
That is the honest empty state the panel exists to show, and it is what makes
a convention mismatch a visible path rather than a permanently idle agent.

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
from pathlib import Path

import pytest

from gitrepo import make_repo_from_template

from autoloop import dashboard
from autoloop.dashboard import (
    ACTION_LOG_DIRNAME,
    ACTION_LOG_EMPTY,
    ACTION_LOG_MISSING,
    ACTION_LOG_PATH_FIELD,
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


def running(task_id=TASK, **fields) -> dict:
    """`state.json` shaped like a loop mid-round: a `task_execution` naming the
    unit in flight, which is the one condition every reader of this panel keys
    off. No `action_log_path` unless the caller adds one."""
    return {
        "phase": "executing",
        "current_task": {"task_id": task_id, "started_at": "2026-09-11T10:00:00+00:00"},
        "task_execution": {
            "task_id": task_id, "task_branch": f"autoloop/{task_id}",
            "worktree_path": "/nonexistent/worker", "task_base_sha": "a" * 40,
            "review_round": 0, **fields,
        },
    }


def convention_path(state_dir, task_id=TASK) -> Path:
    return Path(state_dir) / ACTION_LOG_DIRNAME / f"{task_id}{ACTION_LOG_SUFFIX}"


def write_log(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_repo(tmp_path) -> Path:
    """An observed checkout whose state dir is `<repo>/.autoloop`, said in the
    config the dashboard reads, with a `workers_root` so the inbox `collect()`
    globs is this test's own and not the operator's."""
    repo = make_repo_from_template(tmp_path / "repo", branch="work")
    (repo / ".autoloop").mkdir()
    (repo / ".autoloop" / "config.toml").write_text(
        '[paths]\nstate_dir = ".autoloop"\n'
        f"workers_root = {json.dumps(str(tmp_path / 'workers'))}\n",
        encoding="utf-8",
    )
    return repo


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

    missing = action_log_tail(running(), state_dir)
    assert missing["state"] == "missing"
    assert missing["note"] == ACTION_LOG_MISSING + str(path)
    assert missing["lines"] == [] and missing["tail"] == ""

    # Unreadable, portably: something IS at the path and it is not a file the
    # page may open — a directory here, and a fifo would block the poll.
    path.mkdir(parents=True)
    unreadable = action_log_tail(running(), state_dir)
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


def test_a_log_that_exists_but_cannot_be_opened_says_so_rather_than_nothing_written(tmp_path):
    """The fail-open this panel is about. `Path.is_file()` swallows `OSError`
    and answers `False`, so a permission fault would render as "nothing written
    yet" — the calm sentence, for the state that needs a look. `os.stat`
    succeeds on a mode-0 file and the OPEN raises; the sentence carries the
    OS's reason rather than a paraphrase of it."""
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
    """The five states, each produced, so the vocabulary the page keys off
    cannot drift from what the backend emits. `unlocatable` is the one
    `collect()` cannot reach — an unresolvable state dir reads `state.json` as
    `{}`, so there is no task to look for — and it exists for a caller that
    hands a running record and no directory: the honest answer is "nowhere to
    look", not the convention path resolved against nothing."""
    state_dir = tmp_path / "state"
    produced = {
        action_log_tail(running(), None)["state"],
        action_log_tail(running(), state_dir)["state"],
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
    seconds. Measured on the bytes the reader actually RETURNS, through a spy
    on the one function that opens the file, against a log far larger than the
    budget: the read is bounded by `ACTION_LOG_TAIL_BYTES` whatever the file's
    size, the shown lines are the LAST ones, whole, and the panel says earlier
    lines exist."""
    lines = numbered(150_000)  # ~5 MB
    log = write_log(tmp_path / "big.log", "\n".join(lines) + "\n")
    assert log.stat().st_size > 40 * ACTION_LOG_TAIL_BYTES

    returned = []
    real = dashboard._tail_bytes

    def spy(path, budget):
        data, skipped = real(path, budget)
        returned.append(len(data))
        return data, skipped

    monkeypatch.setattr(dashboard, "_tail_bytes", spy)
    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))

    assert returned and max(returned) <= ACTION_LOG_TAIL_BYTES
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

    returned = []
    real = dashboard._tail_bytes

    def spy(path, budget):
        data, skipped = real(path, budget)
        returned.append(len(data))
        return data, skipped

    monkeypatch.setattr(dashboard, "_tail_bytes", spy)
    view = action_log_tail(running(**{ACTION_LOG_PATH_FIELD: str(log)}))

    assert max(returned) <= ACTION_LOG_TAIL_BYTES
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
