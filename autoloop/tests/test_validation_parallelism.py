"""Validation runs parallel, cache-free, and honest about the `isolated`
marker — in the RUNTIME path, not only in the template.

Post-commit validation re-runs the configured suites against a task's
committed worker repo on EVERY round, revises included. Three properties of
how those commands run are easy to lose and expensive to lose in production:

  * `-n auto` — the whole point. Losing it costs minutes of wall clock per
    round and nothing fails, so nothing would report it.
  * `-p no:cacheprovider` — a failing test writes `.pytest_cache/` into the
    worker repo, and the gate after validation refuses a tree validation
    dirtied. Losing this turns one refusal into two (2026-08-03). Since val-08
    a caller may instead pass a `cache_dir` OUTSIDE the tree, which holds the
    same property with the cache kept; the last section of this file covers
    that path and the first case in it is the guard that the default has not
    moved.
  * the `isolated` marker: selected by exactly one dedicated command (
    `pytest.ini` deselects it from every default run) and never handed an
    xdist worker, since the marker means "this test needs its own process".

**Where these are asserted matters.** They are applied by
`validation.effective_validation_commands`, which every real validation run
funnels through — the configured default, a task's declared `validation`, and
an `execution.validation_commands` record persisted by a session that
dispatched before this existed. So the primary cases below feed the runner a
LEGACY SERIAL list, exactly what an `.autoloop/config.toml` copied from an
older template still holds, and assert on the argv the runner is really
handed. A test that only read `config.example.toml` would pass while every
live session kept running serially — nothing reads that file automatically,
which is precisely the gap this file exists to close. The example config is
still checked, at the end, as a second layer so the template cannot rot.
"""

import configparser
import subprocess
import tomllib
from pathlib import Path

from autoloop.config import load_config
from autoloop.validation import (
    CACHE_DIR_INI,
    RERUN_SELECTION_FLAGS,
    SAFE_VALIDATION_BINARIES,
    effective_validation_command,
    effective_validation_commands,
    run_validation_commands,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "autoloop" / "config.example.toml"

#: What the loop's validation list looked like before this behaviour existed,
#: and what an operator's already-copied config.toml still contains.
LEGACY_SERIAL = (
    ("ruff", "check", "."),
    ("python3", "-m", "pytest", "autoloop/tests", "-q", "-p", "no:cacheprovider"),
    ("python3", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"),
    ("python3", "-m", "pytest", "autoloop/tests", "-q", "-m", "isolated",
     "-p", "no:cacheprovider"),
)


def recording_runner(fail_argv=None):
    """A `subprocess.run` stand-in that records the argv it was handed."""
    seen: list[tuple[str, ...]] = []

    def runner(argv, **kwargs):
        seen.append(tuple(argv))
        failed = fail_argv is not None and tuple(argv) == tuple(fail_argv)
        return subprocess.CompletedProcess(
            argv,
            1 if failed else 0,
            stdout=(
                "FAILED autoloop/tests/test_x.py::test_y - boom\n1 failed\n"
                if failed
                else "ok\n"
            ),
            stderr="",
        )

    return runner, seen


def pairs(argv):
    """Adjacent flag/value pairs, so `-m isolated` is distinguishable from the
    `-m pytest` in `python3 -m pytest` by its VALUE, not by position."""
    return set(zip(argv, argv[1:]))


def is_pytest(argv) -> bool:
    return "pytest" in argv


def is_isolated_run(argv) -> bool:
    return ("-m", "isolated") in pairs(argv)


# ---- the runtime path: what a real validation run actually executes ---------


def test_a_legacy_serial_config_runs_parallel_anyway(tmp_path):
    """The case the feature exists for: a session whose config.toml was copied
    before parallelism existed. Nothing re-reads the template for it, so the
    flag has to be applied where the command is run."""
    runner, seen = recording_runner()
    ok, _summary = run_validation_commands(LEGACY_SERIAL, tmp_path, command_runner=runner)

    assert ok is True
    shared = [argv for argv in seen if is_pytest(argv) and not is_isolated_run(argv)]
    assert len(shared) == 2, f"expected both shared suites to run, saw {seen}"
    for argv in shared:
        assert ("-n", "auto") in pairs(argv), (
            f"{' '.join(argv)} ran serially; post-commit validation is back to the "
            "whole suite on every round, revises included"
        )


def test_the_cache_plugin_is_disabled_even_when_the_config_forgot(tmp_path):
    """`-p no:cacheprovider` is applied for the same reason `-n auto` is: a
    config that predates it would otherwise never gain it, and a failing test
    then dirties the worker repo the tree-clean gate is about to inspect."""
    forgetful = (("python3", "-m", "pytest", "autoloop/tests", "-q"),)
    runner, seen = recording_runner()
    run_validation_commands(forgetful, tmp_path, command_runner=runner)

    (argv,) = seen
    assert ("-p", "no:cacheprovider") in pairs(argv), (
        f"{' '.join(argv)} would write .pytest_cache into the tree it grades"
    )
    assert ("-n", "auto") in pairs(argv)


def test_the_isolated_command_stays_single_process(tmp_path):
    """The marker means "this test needs its own process". Handing it an xdist
    worker alongside others gives back exactly the company it was marked to
    avoid — so this one command must come out serial."""
    runner, seen = recording_runner()
    run_validation_commands(LEGACY_SERIAL, tmp_path, command_runner=runner)

    isolated = [argv for argv in seen if is_isolated_run(argv)]
    assert len(isolated) == 1, f"the isolated marker ran {len(isolated)} times: {seen}"
    offenders = [
        token
        for token in isolated[0]
        if token.startswith("-n") or token.startswith("--numprocesses")
    ]
    assert not offenders, f"the isolated run was parallelised: {offenders!r}"
    assert ("-p", "no:cacheprovider") in pairs(isolated[0])


def test_marker_selection_is_never_rewritten(tmp_path):
    """Parallelism must not silently include or drop the `isolated` marker.
    Whatever each command selected before, it selects after."""
    runner, seen = recording_runner()
    run_validation_commands(LEGACY_SERIAL, tmp_path, command_runner=runner)

    def markers(argv):
        return [value for flag, value in zip(argv, argv[1:]) if flag == "-m"]

    for configured, effective in zip(LEGACY_SERIAL, seen):
        assert markers(effective) == markers(configured), (
            f"{' '.join(configured)} now selects a different set of tests: "
            f"{' '.join(effective)}"
        )


def test_an_explicit_worker_count_is_never_overridden(tmp_path):
    """`-n 4` and `-n 0` are operator decisions — `-n 0` in particular is how
    you say "run this one in-process". Neither may be second-guessed, and
    neither may end up with two conflicting `-n` flags."""
    explicit = (
        ("python3", "-m", "pytest", "tests/", "-n", "4"),
        ("python3", "-m", "pytest", "tests/", "-n", "0"),
        ("pytest", "tests/", "--numprocesses=2"),
    )
    runner, seen = recording_runner()
    run_validation_commands(explicit, tmp_path, command_runner=runner)

    for configured, effective in zip(explicit, seen):
        assert effective.count("-n") == configured.count("-n")
        assert ("-n", "auto") not in pairs(effective), f"overrode {' '.join(configured)}"


def test_non_pytest_commands_are_returned_untouched(tmp_path):
    """Only pytest understands these flags. A structural check — not "is
    'pytest' somewhere in the argv" — keeps a `python3 -c` probe (the real one
    in `test_validation_env.py` dials a Postgres listener) from being handed
    pytest arguments it would die on."""
    others = (
        ("ruff", "check", "."),
        ("npx", "vitest", "run"),
        ("python3", "-c", "import sys; sys.exit(0)"),
        ("npx", "tsc", "--noEmit"),
    )
    runner, seen = recording_runner()
    run_validation_commands(others, tmp_path, command_runner=runner)
    assert tuple(seen) == others


def test_normalization_is_idempotent():
    """A record persisted by a session that already normalized (or a config an
    operator updated by hand) must not collect a second `-n auto`."""
    once = effective_validation_commands(LEGACY_SERIAL)
    assert effective_validation_commands(once) == once
    for argv in once:
        assert argv.count("-n") <= 1, f"two worker-count flags in {' '.join(argv)}"
        if is_pytest(argv):
            assert argv.count("-p") == 1, f"duplicated plugin flag in {' '.join(argv)}"


def test_a_double_dash_command_still_collects_its_paths():
    """Flags are INSERTED after the `pytest` token, not appended: everything
    after a bare `--` is a path, so an appended `-n auto` would become two
    filenames pytest cannot collect."""
    argv = effective_validation_command(("pytest", "-q", "--", "tests/weird -name.py"))
    assert argv[-1] == "tests/weird -name.py"
    assert argv.index("-n") < argv.index("--")


# ---- the runtime path, end to end -------------------------------------------


def test_a_config_file_on_disk_reaches_the_runner_parallel(tmp_path):
    """`load_config` → `audit.validation_commands` → `run_validation_commands`
    is the chain a live loop uses for every task that declares no validation of
    its own. Driven from a real TOML file holding the OLD serial commands."""
    def as_toml(argv):
        return "[" + ", ".join(f'"{token}"' for token in argv) + "]"

    body = ",\n  ".join(as_toml(argv) for argv in LEGACY_SERIAL)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[browser]\nconversation_url = "https://chatgpt.com/c/abc123"\n\n'
        '[paths]\nworkers_root = "/tmp/al-workers"\n\n'
        f"[audit]\nvalidation_commands = [\n  {body}\n]\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.audit.validation_commands == LEGACY_SERIAL, (
        "the config is preserved as written — normalization happens at RUN time, "
        "so an operator's file never silently disagrees with itself"
    )

    runner, seen = recording_runner()
    run_validation_commands(config.audit.validation_commands, tmp_path, command_runner=runner)
    shared = [argv for argv in seen if is_pytest(argv) and not is_isolated_run(argv)]
    assert shared and all(("-n", "auto") in pairs(argv) for argv in shared)


def test_a_task_declared_command_is_parallelised_by_the_executor(tmp_path):
    """A task that declares its own `validation` (the backend suite, say) runs
    through `ImplementExecutor`, not through the configured default — so the
    normalization has to sit under that path too, which it does by living in
    the shared runner both ends call."""
    from autoloop.audit.agents import AgentResult
    from autoloop.contract import Decision, Directive
    from autoloop.implement_executor import ImplementExecutor
    from autoloop.tasks import Task

    class FakeAgent:
        def run(self, spec):
            return AgentResult(
                domain=spec.domain, raw_text="edited", returncode=0,
                duration_seconds=0.1, command=("claude",),
            )

    class FakeGit:
        repo_root = tmp_path

        def dirty_paths_all(self):
            return ["lexy-app/backend/routers/books.py"]

    declared = ("python3", "-m", "pytest", "-q")
    runner, seen = recording_runner()
    executor = ImplementExecutor(
        git=FakeGit(),
        agent_runner=FakeAgent(),
        validation_commands=(("ruff", "check", "."),),
        command_runner=runner,
        # Since advis-01 (2026-08-26) a round whose agent never uses the advisory
        # channel is handed back once and then WITHHELD from review, and a
        # withheld round never launches the command whose flags this test reads.
        # `FakeAgent` cannot ask, so the contract is pinned off here and graded in
        # `test_agent_self_validation.py` §10a.
        advisory_zero_call_returns=0,
    )
    outcome = executor.execute(
        Directive(decision=Decision.IMPLEMENT, reason="do it", task_id="rt-01"),
        Task(id="rt-01", title="t", description="d", validation=(declared,)),
    )

    assert outcome.status == "ok", outcome.summary
    assert seen == [effective_validation_command(declared)]
    assert ("-n", "auto") in pairs(seen[0])
    assert ("-p", "no:cacheprovider") in pairs(seen[0])


def test_a_parallel_run_still_reports_pass_fail_per_command(tmp_path):
    """Parallelism lives inside one command; the summary is still one
    PASS/FAIL report per command, naming the command that really ran, so a
    failure is attributable to one suite rather than to "pytest".

    Driven with `fail_fast=False` so this stays a test about PARALLELISM: the
    default stops at the first failing command and reports the rest as NOT RUN
    (val-03), which is asserted in `test_validation_failfast.py`. Every command
    running is exactly the full-run mode, so this doubles as an exercise of it
    from a test that predates it.
    """
    effective = effective_validation_commands(LEGACY_SERIAL)
    failing = next(a for a in effective if is_pytest(a) and not is_isolated_run(a))
    runner, _seen = recording_runner(fail_argv=failing)

    ok, summary = run_validation_commands(
        LEGACY_SERIAL, tmp_path, command_runner=runner, fail_fast=False
    )

    assert ok is False
    # Asserted per command rather than by counting segments: `failure_digest`
    # joins its own lines with "; " as well, so splitting the summary on that
    # separator would make the count depend on the interior text of a digest.
    assert f"{' '.join(failing)}: FAIL" in summary
    assert "test_x.py::test_y" in summary, "the failing test must still be named"
    for argv in effective:
        if argv == failing:
            continue
        assert f"{' '.join(argv)}: PASS" in summary


def test_parallelism_is_never_hidden_in_addopts():
    """`-n auto` belongs on the individual command, not in `pytest.ini`.

    In `addopts` it would reach every invocation — including the dedicated
    isolated run, and the `--collect-only` subprocess in `test_crash_safety.py`
    that asks pytest which tests carry the marker.
    """
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    addopts = parser.get("pytest", "addopts", fallback="")
    assert " -n" not in f" {addopts}", f"parallelism leaked into addopts: {addopts!r}"
    assert "--numprocesses" not in addopts, f"same, spelled out: {addopts!r}"
    # The complement, and the reason the dedicated command has to exist at all.
    assert "not isolated" in addopts, f"default run must exclude it; addopts={addopts!r}"


# ---- second layer: the template an operator copies --------------------------


def shipped_commands() -> list[tuple[str, ...]]:
    """`[audit].validation_commands` from the shipped example config."""
    with EXAMPLE_CONFIG.open("rb") as handle:
        data = tomllib.load(handle)
    commands = data["audit"]["validation_commands"]
    assert isinstance(commands, list) and commands, "the example ships no validation"
    # The same shape `config.load_config` enforces on the way in — a list that
    # fails this would raise ConfigError for whoever copied the file.
    for argv in commands:
        assert isinstance(argv, list) and argv, f"not a non-empty argv list: {argv!r}"
        assert all(isinstance(token, str) for token in argv), f"non-string in {argv!r}"
    return [tuple(argv) for argv in commands]


def test_the_shipped_commands_are_all_launchable():
    """A shipped command `run_validation_commands` would REFUSE is worse than
    no command: it reports a failure the operator did not cause and cannot fix
    by fixing their code."""
    for argv in shipped_commands():
        binary = Path(argv[0]).name
        assert binary in SAFE_VALIDATION_BINARIES, (
            f"{binary!r} is not in SAFE_VALIDATION_BINARIES, so {argv!r} would be "
            "refused unrun"
        )


def test_the_shipped_isolated_command_still_exists():
    """`pytest.ini` deselects the marker from every default run, so without a
    dedicated command in the template that coverage runs nowhere while every
    suite stays green. Normalization cannot supply this one — it changes how a
    command runs, never which commands exist."""
    isolated = [argv for argv in shipped_commands() if is_isolated_run(argv)]
    assert len(isolated) == 1, (
        "exactly one shipped command must run the `isolated` marker — "
        f"found {len(isolated)}"
    )


def test_the_shipped_list_needs_no_repair_at_run_time():
    """The template already says what the runtime would otherwise apply, so a
    fresh deployment's config reads as what it does. A mismatch here means the
    template has drifted behind the runtime, not that anything runs serially."""
    shipped = shipped_commands()
    assert effective_validation_commands(shipped) == tuple(shipped), (
        "the runtime would rewrite the shipped list, so the template no longer "
        "reads as what a run does — update autoloop/config.example.toml's "
        "[audit].validation_commands to match the rule in "
        "validation.effective_validation_command (the runtime is not the thing "
        "to change here; nothing runs serially either way)"
    )


# ---- val-08: the cache is RELOCATED for an advisory run, never disabled ------
#
# `-p no:cacheprovider` is still the default and is still what every VERDICT run
# carries — the first two cases below are the regression guard for that. What is
# added is a `cache_dir` a caller may pass, which moves pytest's cache OUT of the
# tree being graded instead of switching the plugin off, and a `rerun_last_failed`
# that only means anything once it is on.
#
# These are argv tests. The tree-level proof — a REAL failing pytest run under a
# relocated cache leaves the worker repo byte-identical — needs a real repository
# and lives in `test_agent_self_validation.py`.

CACHE = "/tmp/autoloop-val08-test-cache"

#: A pytest command shaped like the SHIPPED ones: it spells out both flags the
#: runtime would otherwise inject. Everything about the replacement rule below
#: is about this shape, because it is the only one production ever runs — a rule
#: that only fired on a command WITHOUT `-p no:cacheprovider` would pass every
#: synthetic test here and be inert on every real deployment.
SHIPPED_SHAPE = (
    "python3", "-m", "pytest", "autoloop/tests", "-q", "-n", "auto",
    "-p", "no:cacheprovider",
)


def ini_overrides(argv) -> set[str]:
    """Every `-o name=value` this argv sets, in any of pytest's spellings."""
    found = set()
    for index, token in enumerate(argv):
        if token in ("-o", "--override-ini") and index + 1 < len(argv):
            found.add(argv[index + 1])
        elif token.startswith("--override-ini="):
            found.add(token[len("--override-ini="):])
        elif token.startswith("-o") and len(token) > 2 and not token.startswith("--"):
            found.add(token[2:])
    return found


def rerun_flags(argv) -> set[str]:
    """Every cache-driven reselection flag in `argv`, whatever its spelling.

    Read off `RERUN_SELECTION_FLAGS` rather than spelled here: a check written
    as `"--lf" not in argv` passes on `--last-failed`, which is the shape of
    hole this whole set exists to close.
    """
    return {token for token in argv if token in RERUN_SELECTION_FLAGS}


def test_the_default_still_disables_the_cache_everywhere():
    """The 2026-08-03 property, unconditional on the path nothing passes a cache
    to. This is the regression guard for requirement 6: pass no `cache_dir` and
    every pytest command is exactly what it was."""
    for argv in effective_validation_commands(LEGACY_SERIAL):
        if not is_pytest(argv):
            continue
        assert ("-p", "no:cacheprovider") in pairs(argv)
        assert not ini_overrides(argv)
        assert not rerun_flags(argv)


def test_a_cache_dir_replaces_the_flag_that_would_make_it_inert():
    """The load-bearing case, and the one a smaller rule would fail.

    Every SHIPPED pytest command spells `-p no:cacheprovider` out — the fixed
    point `test_the_shipped_list_needs_no_repair_at_run_time` asserts. With the
    plugin off, `cache_dir` does nothing and `--lf` is an unrecognised argument
    that exits 4, so a rule that deferred to the existing flag would leave the
    feature working only on commands production does not run.
    """
    argv = effective_validation_command(SHIPPED_SHAPE, cache_dir=CACHE)
    assert ("-p", "no:cacheprovider") not in pairs(argv)
    assert "no:cacheprovider" not in argv
    assert f"{CACHE_DIR_INI}={CACHE}" in ini_overrides(argv)
    assert ("-n", "auto") in pairs(argv), "the worker count must be untouched by this"
    assert "autoloop/tests" in argv, "the command's own test paths must survive"


def test_the_attached_spelling_of_the_disabling_flag_is_removed_too():
    """`-pno:cacheprovider` is the same instruction as `-p no:cacheprovider`, and
    a survivor would silently switch the relocated cache back off."""
    argv = effective_validation_command(
        ("pytest", "tests/", "-q", "-n", "0", "-pno:cacheprovider"), cache_dir=CACHE
    )
    assert "-pno:cacheprovider" not in argv
    assert f"{CACHE_DIR_INI}={CACHE}" in ini_overrides(argv)


def test_a_rerun_cannot_be_asked_for_without_a_cache():
    """FAIL CLOSED, structurally. `--lf` is served by the cacheprovider plugin,
    so `--lf` next to `-p no:cacheprovider` is `exit 4` before a test runs. The
    injection therefore sits INSIDE the branch that turned the cache on: asking
    for a rerun with no cache is not an error to handle, it is a state that
    cannot be reached."""
    argv = effective_validation_command(SHIPPED_SHAPE, rerun_last_failed=True)
    assert not rerun_flags(argv)
    assert ("-p", "no:cacheprovider") in pairs(argv)
    assert argv == tuple(SHIPPED_SHAPE), "with no cache this must be a no-op"


def test_a_rerun_with_a_cache_adds_exactly_one_lf():
    argv = effective_validation_command(
        SHIPPED_SHAPE, cache_dir=CACHE, rerun_last_failed=True
    )
    assert rerun_flags(argv) == {"--lf"}
    assert argv.count("--lf") == 1
    assert f"{CACHE_DIR_INI}={CACHE}" in ini_overrides(argv)


def test_no_second_rerun_flag_is_added_on_top_of_one_already_there():
    """Whatever spelling a caller wrote, this adds nothing beside it — two
    reselection flags is a command whose meaning nobody wrote down."""
    for flag in sorted(RERUN_SELECTION_FLAGS):
        argv = effective_validation_command(
            ("pytest", "tests/", "-q", "-n", "0", flag),
            cache_dir=CACHE,
            rerun_last_failed=True,
        )
        assert rerun_flags(argv) == {flag}, f"{flag} collected a companion"


def test_a_command_that_names_its_own_cache_dir_is_left_exactly_as_written():
    """An operator who has said where their cache goes has said it, in any of
    pytest's four spellings. Nothing is injected then — not `-o cache_dir`, and
    not `-p no:cacheprovider` either, which would disable the location they
    chose — and no `--lf` is added to a command this did not set up."""
    for declaration in (
        ("-o", "cache_dir=/elsewhere"),
        ("-ocache_dir=/elsewhere",),
        ("--override-ini", "cache_dir=/elsewhere"),
        ("--override-ini=cache_dir=/elsewhere",),
    ):
        argv = ("pytest", "tests/", "-q", "-n", "auto") + declaration
        assert effective_validation_command(
            argv, cache_dir=CACHE, rerun_last_failed=True
        ) == argv, f"{declaration} was not respected"


def test_an_operator_cache_dir_is_still_disabled_when_no_cache_is_asked_for():
    """The verdict path is unchanged for that command too: with no `cache_dir`
    passed, `-p no:cacheprovider` is injected exactly as it was before val-08,
    so the tree-cleanliness rule does not depend on what an operator wrote."""
    argv = effective_validation_command(("pytest", "tests/", "-q", "-n", "0",
                                         "-o", "cache_dir=/elsewhere"))
    assert ("-p", "no:cacheprovider") in pairs(argv)


def test_normalization_with_a_cache_is_idempotent():
    """A second pass must not collect a second `-o cache_dir`, a second `--lf`,
    or — the dangerous one — a `-p no:cacheprovider` on top of the cache the
    first pass turned on."""
    once = effective_validation_commands(
        LEGACY_SERIAL, cache_dir=CACHE, rerun_last_failed=True
    )
    twice = effective_validation_commands(
        once, cache_dir=CACHE, rerun_last_failed=True
    )
    assert twice == once
    for argv in once:
        if not is_pytest(argv):
            continue
        assert "no:cacheprovider" not in argv
        assert argv.count("--lf") == 1
        assert len(ini_overrides(argv)) == 1


def test_the_isolated_command_stays_serial_under_a_cache():
    """The marker means "this test needs its own process". Relocating the cache
    must not change which run gets an xdist worker."""
    isolated = ("python3", "-m", "pytest", "autoloop/tests", "-q", "-m", "isolated",
                "-p", "no:cacheprovider")
    argv = effective_validation_command(isolated, cache_dir=CACHE)
    assert "-n" not in argv
    assert f"{CACHE_DIR_INI}={CACHE}" in ini_overrides(argv)


def test_a_non_pytest_command_is_untouched_by_a_cache():
    for argv in (("ruff", "check", "."), ("npx", "vitest", "run"),
                 ("python3", "-c", "import pytest")):
        assert effective_validation_command(
            argv, cache_dir=CACHE, rerun_last_failed=True
        ) == argv


def test_the_cache_reaches_the_real_launcher_and_the_summary(tmp_path):
    """Asserted on what was HANDED to the launcher, not on what the caller
    meant: the agent reads `summary`, so the flags its run carried have to be
    visible there rather than inferred."""
    runner, seen = recording_runner()
    _ok, summary = run_validation_commands(
        LEGACY_SERIAL,
        tmp_path,
        command_runner=runner,
        pytest_cache_dir=CACHE,
        rerun_last_failed=True,
    )
    launched = [argv for argv in seen if is_pytest(argv)]
    assert launched and all(
        f"{CACHE_DIR_INI}={CACHE}" in ini_overrides(argv) for argv in launched
    )
    assert all("no:cacheprovider" not in argv for argv in launched)
    assert f"{CACHE_DIR_INI}={CACHE}" in summary and "--lf" in summary
