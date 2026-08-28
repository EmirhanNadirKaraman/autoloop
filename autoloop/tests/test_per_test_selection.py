"""`[audit] test_selection = "per_test"`: narrowing WITHIN the selected files.

`reachable` picks whole test files. Measured on this checkout, that narrows very
little — a change to `autoloop/dashboard.py` selects 72 of 93 files — because
most tests in a selected file never touch the changed module. This mode keeps
that file set exactly and deselects the individual tests `per_test_deps` says
cannot reach the change.

The claims that matter are about what it REFUSES to drop, so most of them are
here rather than in the measurement: the file set must not move, a file the
commit itself changed must keep every test, a file selected by ATTRIBUTION
rather than reachability must keep every test, and a list that cannot be written
must run everything. Dropping a test that does exercise the change is a silent
wrong answer, and `scripts/check_selection_soundness.py` is the CI job that
proves the analysis underneath does not.
"""

from __future__ import annotations

from pathlib import Path

from autoloop.validation import (
    TEST_SELECTION_FULL,
    TEST_SELECTION_PER_TEST,
    TEST_SELECTION_REACHABLE,
    select_validation_commands,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUFF = ("ruff", "check", ".")
SUITE = ("python3", "-m", "pytest", "autoloop/tests", "-q", "-n", "auto")
COMMANDS = (RUFF, SUITE)

#: A real-repository selection walks the whole checkout. Cached per process for
#: the reason `test_prose_doc_selection.py` caches its own: several tests below
#: ask about the same two rounds and the tree does not change while they run.
_ROUNDS: dict[tuple[str, str], object] = {}


def round_for(path: str, mode: str):
    key = (path, mode)
    if key not in _ROUNDS:
        _ROUNDS[key] = select_validation_commands(COMMANDS, [path], REPO_ROOT, mode=mode)
    return _ROUNDS[key]


def pytest_command(selection) -> tuple[str, ...]:
    return next(c for c in selection.commands if "pytest" in c)


def nested_pytest(target: Path, *args: str):
    """Run pytest in a CHILD process, isolated from the run that spawned it.

    Three things this must not inherit, each of which broke it once:

    * **`pytest-randomly`.** Passing a path outside the checkout makes pytest
      compute ROOTDIR FROM THAT PATH, so the repository's `pytest.ini` is never
      read and its `-p no:randomly` never applies — and that plugin is installed
      here, so omitting it changes behaviour (pytest.ini says so). The child then
      seeded numpy from the inherited xdist environment and numpy refused the
      value: `ValueError: Seed must be between 0 and 2**32 - 1`. Passed
      explicitly rather than relied upon.
    * **the parent's xdist environment.** `PYTEST_XDIST_*` and
      `PYTEST_CURRENT_TEST` describe the OUTER run. A child that reads them
      believes it is a worker of a session it is not part of — which is what
      produced the seed above.
    * **the parent's working directory.** `cwd` is the target, and the package
      is reached through `PYTHONPATH`, so nothing here depends on what the other
      seven xdist workers are doing to the checkout at the same moment.
    """
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(target), "-q",
         "-p", "no:cacheprovider", "-p", "no:randomly", *args],
        cwd=str(target), env=env, capture_output=True, text=True,
    )


# ---- the file set must not move ---------------------------------------------


def test_per_test_selects_exactly_the_files_reachable_does():
    """The mode narrows WITHIN files; it must not change which files are chosen.

    Asserted as an equality rather than as "it still selects something": a count
    would pass just as well if the set had been quietly replaced.
    """
    reachable = round_for("autoloop/dashboard.py", TEST_SELECTION_REACHABLE)
    per_test = round_for("autoloop/dashboard.py", TEST_SELECTION_PER_TEST)
    assert per_test.selected == reachable.selected
    assert not per_test.widened and not reachable.widened


def test_only_per_test_deselects_anything():
    assert round_for("autoloop/dashboard.py", TEST_SELECTION_REACHABLE).deselected == 0
    assert round_for("autoloop/dashboard.py", TEST_SELECTION_PER_TEST).deselected > 0


def test_full_still_widens_and_drops_nothing():
    chosen = select_validation_commands(
        COMMANDS, ["autoloop/dashboard.py"], REPO_ROOT, mode=TEST_SELECTION_FULL
    )
    assert chosen.widened and chosen.deselected == 0
    assert chosen.commands == COMMANDS


# ---- the command, and where the list lives ----------------------------------


def test_the_command_loads_the_plugin_and_names_the_list():
    argv = pytest_command(round_for("autoloop/dashboard.py", TEST_SELECTION_PER_TEST))
    assert "-p" in argv and "autoloop.pytest_deselect" in argv
    index = argv.index("--autoloop-deselect")
    listed = Path(argv[index + 1])
    assert listed.is_file()
    # OUTSIDE the checkout: validation runs in the task's worker repository and
    # the gate after it refuses a tree validation dirtied, so a list written
    # beside the tests would fail the very round it narrowed.
    assert REPO_ROOT not in listed.parents


def test_reachable_adds_no_plugin_to_the_command():
    argv = pytest_command(round_for("autoloop/dashboard.py", TEST_SELECTION_REACHABLE))
    assert "autoloop.pytest_deselect" not in argv
    assert "--autoloop-deselect" not in argv


def test_the_list_names_tests_by_function_without_parametrisation():
    argv = pytest_command(round_for("autoloop/dashboard.py", TEST_SELECTION_PER_TEST))
    listed = Path(argv[argv.index("--autoloop-deselect") + 1])
    lines = [line for line in listed.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "a round reporting deselections must have written them"
    for line in lines:
        path, _, name = line.partition("::")
        assert path.startswith("autoloop/tests/") and name
        # Every case of one test function shares that function's dependencies,
        # so the list names the function and the plugin drops all of its cases.
        assert "[" not in name


def test_the_same_round_reuses_one_list_file():
    """The path is the hash of its own contents, so a round is a pure function of
    what it selected — two identical rounds do not litter two files."""
    first = pytest_command(round_for("autoloop/dashboard.py", TEST_SELECTION_PER_TEST))
    again = select_validation_commands(
        COMMANDS, ["autoloop/dashboard.py"], REPO_ROOT, mode=TEST_SELECTION_PER_TEST
    )
    assert pytest_command(again) == first


# ---- what it refuses to drop ------------------------------------------------


def test_a_changed_test_file_keeps_every_one_of_its_tests():
    """The commit edited that file; its own tests are the thing under test, and
    no module-level reasoning describes an edit to a test."""
    changed = "autoloop/tests/test_lock.py"
    chosen = round_for(changed, TEST_SELECTION_PER_TEST)
    argv = pytest_command(chosen)
    if "--autoloop-deselect" not in argv:
        return  # nothing was dropped anywhere, which satisfies the claim
    listed = Path(argv[argv.index("--autoloop-deselect") + 1])
    dropped = listed.read_text(encoding="utf-8")
    assert f"{changed}::" not in dropped


def test_a_prose_change_drops_nothing_because_attribution_is_about_files():
    """A changed `.md` is attributed the files that READ it. That is a claim
    about the FILE naming a document, not about which of its tests do, and the
    seeds are not modules — so reachability cannot answer for them and nothing
    inside them may be dropped."""
    chosen = round_for("docs/TESTS.md", TEST_SELECTION_PER_TEST)
    assert not chosen.widened
    assert chosen.deselected == 0


# ---- evidence ---------------------------------------------------------------


def test_the_evidence_states_the_deselection_and_its_conditions():
    evidence = round_for("autoloop/dashboard.py", TEST_SELECTION_PER_TEST).evidence()
    assert "DESELECTED" in evidence
    assert 'test_selection = "per_test"' in evidence
    assert "is not opaque" in evidence


def test_reachable_evidence_says_nothing_about_deselection():
    assert "DESELECTED" not in round_for(
        "autoloop/dashboard.py", TEST_SELECTION_REACHABLE
    ).evidence()


# ---- the plugin itself ------------------------------------------------------


def test_an_unreadable_list_drops_nothing(tmp_path):
    """A list that cannot be read must never be treated as "drop nothing you can
    see" — the answer is to run everything."""
    from autoloop.pytest_deselect import _entries

    assert _entries(tmp_path / "does-not-exist.txt") == frozenset()
    empty = tmp_path / "empty.txt"
    empty.write_text("\n  \n", encoding="utf-8")
    assert _entries(empty) == frozenset()


def test_the_plugin_drops_the_named_tests_and_every_case_of_them(tmp_path):
    """End to end through a real pytest run: the list names FUNCTIONS, and a
    parametrised function's cases all go with it."""
    (tmp_path / "test_demo.py").write_text(
        "import pytest\n"
        "def test_kept(): assert True\n"
        "def test_dropped(): assert False\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_cases(x): assert False\n",
        encoding="utf-8",
    )
    listed = tmp_path / "drop.txt"
    listed.write_text("test_demo.py::test_dropped\ntest_demo.py::test_cases\n", encoding="utf-8")

    proc = nested_pytest(
        tmp_path, "-p", "autoloop.pytest_deselect", "--autoloop-deselect", str(listed)
    )
    # The dropped ones would FAIL if they ran, so a green run is the assertion.
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert "1 passed" in proc.stdout and "3 deselected" in proc.stdout


def test_the_plugin_is_inert_when_no_list_is_named(tmp_path):
    """Loading it must not be a change in behaviour by itself."""
    (tmp_path / "test_demo.py").write_text("def test_one(): assert True\n", encoding="utf-8")
    proc = nested_pytest(tmp_path, "-p", "autoloop.pytest_deselect")
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert "1 passed" in proc.stdout and "deselected" not in proc.stdout
