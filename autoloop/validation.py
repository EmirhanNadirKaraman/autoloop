"""Shared validation-command execution.

Two call sites run configured "validation" commands (lint/tests) against a
working tree and need to agree on the same safety boundary: the audit
executor (`audit/executor.py`, summarizing repo health) and the
produce-then-review post-commit check (`orchestrator.py`, re-running
validation against a task's own worktree AFTER its commit exists — a
pre-commit hook can change committed content in ways pre-commit validation
never saw, so the check must re-run for real rather than trusting the
executor's own report).

Both refuse to launch anything outside `SAFE_VALIDATION_BINARIES` — validation
runs repo CHECKS, never repo mutations. This module owns the one definition
so the binary allowlist cannot drift between the two call sites.

**The ENVIRONMENT a validation command runs under is owned here too, and it
is always explicit.** `run_validation_commands` never lets a subprocess simply
inherit `os.environ`: it passes `strip_validation_vars(os.environ)` — the
parent environment minus every name in `VALIDATION_ENV_ALLOWLIST` — and then
overlays the operator's `ValidationEnv` when one is configured. That ordering
is the whole boundary: the configured file is the ONLY way a database
credential can reach a validation run, so an operator who sources `.env` into
the loop's shell does not silently change what validation connects to, and a
run with no file configured fails honestly rather than picking up ambient
credentials. Summaries are redacted through the same `ValidationEnv` before
they are returned, because the returned string becomes
`state.last_validation` — which reaches `state.json`, the transcript, blocker
records, and the review packet sent to the reviewer.

**The pytest flags a validation run needs are owned here too** — see
`effective_validation_commands` below. They are applied to whatever the caller
passes rather than left to the config file, because the config file is copied
once per deployment and then never re-read from the template: an operator whose
`.autoloop/config.toml` predates a flag would keep running without it forever,
and every path into a validation run (the configured default, a task's declared
`validation`, and an `execution.validation_commands` record persisted by a
session that dispatched before the flag existed) funnels through this function.

**The pytest CACHE is a LOCATION question, not an on/off one** (val-08,
2026-08-31). `NO_CACHE_ARGS` is still what a run carries by default and still
what every VERDICT run carries, for the reason that constant states. But the
2026-08-03 defect it was written for is that pytest wrote `.pytest_cache/` INTO
the worker tree a gate was about to inspect, and `cache_dir` is an ini option
that takes an absolute path — so a caller that has somewhere outside the tree to
put it can keep the feature and keep the tree clean. Exactly one caller does:
`implement_executor.AdvisoryValidation`, whose runs are the agent's own and
whose second run in a round wants `--lf` (rerun only what failed). It passes
`cache_dir=` and, on a run that follows a failed one, `rerun_last_failed=True`.
Every other caller passes neither and gets byte-identical behaviour to before.

The two are mutually exclusive by construction rather than by convention: with
the cacheprovider plugin disabled, `cache_dir` does nothing and `--lf` is an
unrecognised argument that exits 4 before a test runs. So `--lf` can only be
injected inside the branch that turned the cache ON, and a caller cannot ask for
one without the other.

**A `cache_dir` a caller passes REPLACES everything the command said about the
cache, a configured `cache_dir` included** (found in review, 2026-08-31). The
first version of this deferred to an explicit one — "an operator who has said
where their cache goes has said it" — and that is wrong twice over. A configured
RELATIVE path resolves against pytest's rootdir, i.e. INTO the worker tree the
post-commit gate is about to inspect, and with the disabling flag no longer
injected beside it that is the 2026-08-03 defect restored in full. A configured
ABSOLUTE path is worse in the other direction: it is one fixed location, so every
task's advisory run would read and write one `lastfailed`, and one round's
failures would decide which tests an unrelated round re-runs. Neither is a
location an advisory run may use, however explicitly it was written, so an
advisory run has exactly two outcomes — the directory `AdvisoryValidation` minted
and validated, or `NO_CACHE_ARGS` with no `--lf` if anything the rewrite could
not parse still mentions a cache policy. That second arm is the fail-closed one
and it is a SUBSTRING test (`_mentions_cache_policy`), because a refusal that
depended on recognising a spelling would be switched off by the first spelling it
did not recognise.

**HOW FAR a run gets is owned here too** — see `run_validation_commands`'s
`fail_fast` parameter. A validation run answers "is this approvable?", and the
FIRST failing command settles that; everything after it is paid for against a
verdict already decided. So the default stops there and reports the rest as
`NOT RUN`, which is deliberately DIFFERENT information from `PASS` — the
reviewer decides partly on what was exercised. `fail_fast=False` runs
everything, for the other question ("how much is broken?"). The measurements
that motivated it are in `docs/AUTOLOOP.md` §4h.

**WHICH tests a per-commit run needs is decided here too** — see
`select_validation_commands` and the "per-commit test selection" section at the
bottom of this module. That is a strictly separate function from the flag
normalization above and deliberately so: `effective_validation_command` is
asserted idempotent and depends on nothing but its argv and the two policy
values a caller may hand it (a pytest `cache_dir`, and whether this run is an
advisory rerun) — it still reads no file, creates no directory and consults no
environment, so it stays a pure argv rewrite; the directory those flags name is
created by `AdvisoryValidation`, the one caller that asks for one. Selection, by
contrast, reads the repository's import graph and the commit's changed paths.
Folding the two together would make a pure argv rewrite depend on the
filesystem.

`select_validation_commands` is deliberately PHASE-AGNOSTIC: it takes a command
list, a set of changed repo-relative paths and a repo root, and knows nothing
about commits. BOTH call sites satisfy that signature and BOTH consume it since
val-04 (2026-08-27): `implement_executor._select_validation` before the commit,
from `git.dirty_paths_all()` and the worker-repo root, and
`orchestrator._run_post_commit_validation` after it, from git's own account of
the commit range. Adopting it at the second site needed no change here at all,
which is what phase-agnostic was for.

The consequence is the one `PRECOMMIT_EVIDENCE` below has to state rather than
imply: a full-suite run is no longer GUARANTEED at either phase, and a round
that narrows at both has none at all. The pre-commit run used to be an
independent full backstop under the narrowed post-commit re-run, and it is not
one now — the two runs share this model, this command list and (bar the commit
itself) these inputs, so they are correlated rather than independent. Each phase
still reports its own decision where it ran, so the two can disagree honestly:
one widening does not make the other's evidence false.
`test_test_selection.py` pins both ends.

Caveat for anyone editing `audit/executor.py`: that module has its OWN
validation runner (`AuditExecutor._run_validation`) which shares
`SAFE_VALIDATION_BINARIES` but not this function. It runs read-only audit
checks with no writer involved and deliberately gets NO credentials — and no
flag normalization either, since it grades the checkout rather than a worker
repo a gate is about to inspect. It also runs EVERY configured command and is
untouched by the fail-fast default above: it asks "how much is broken?" about a
checkout, not "is this candidate approvable?". If that ever needs to change,
route it through this function rather than growing a second policy there.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from .per_test_deps import dependencies_by_test
from .validation_env import ValidationEnv, redact_with, strip_validation_vars

#: Validation commands may only start with these binaries.
SAFE_VALIDATION_BINARIES = frozenset(
    {"ruff", "pytest", "python", "python3", "npm", "npx", "tsc"}
)


#: Lines that NAME what failed, as opposed to counting it. pytest prints
#: these in its short summary; unittest and ruff use the same leading words.
_FAILURE_LINE_RE = re.compile(r"^(FAILED|ERROR)\b")

#: Terminal colour codes. pytest emits them even under `-q` when it thinks it
#: has a tty, and they survive `capture_output` into blocker text and park
#: messages, where they render as literal `\x1b[31m` noise.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: Bounds on the digest. Generous enough to name a dozen failing tests,
#: bounded so a catastrophic run cannot flood a park message or a review
#: packet with tool output.
_DIGEST_MAX_LINES = 12
_DIGEST_MAX_CHARS = 700

#: What a command that never launched is reported as. Deliberately NOT
#: "SKIPPED": `TestSelection.skipped` already means "dropped by test selection,
#: no reachable test under its paths", and `_run_post_commit_validation`
#: concatenates both strings into ONE reviewer-facing summary.
NOT_RUN = "NOT RUN"

#: One xdist worker per core. Validation re-runs the whole configured suite
#: against the committed worker repo on EVERY round, revises included, against
#: a change that usually touches two or three files — serially that is minutes
#: of wall clock per round, and nothing about it FAILS, so nothing would ever
#: report it. Needs `pytest-xdist` in the interpreter the loop invokes (the
#: root `requirements.txt` pulls it in via `lexy-app/backend/requirements.txt`);
#: without it pytest exits 4 on an unrecognised argument and every task fails
#: validation at once, which is the first thing to check if that ever happens.
PARALLEL_ARGS: tuple[str, ...] = ("-n", "auto")

#: pytest writes `.pytest_cache/` the moment a test fails. Validation runs
#: inside the task's own worker repo and the gate right after it refuses a
#: worktree validation dirtied, so one failing test produced two refusals
#: instead of one (2026-08-03). Disabling the plugin keeps a failing run from
#: mutating the tree it is grading.
#:
#: STILL THE DEFAULT and still what every VERDICT run carries: a caller that
#: passes no `cache_dir` gets exactly this and nothing else has moved. What
#: val-08 added is the OTHER way of holding the same property — see
#: `CACHE_DIR_INI`, and the module docstring for why the two are exclusive.
NO_CACHE_ARGS: tuple[str, ...] = ("-p", "no:cacheprovider")

#: The pytest ini option that decides WHERE the cache is written. It accepts an
#: absolute path, which is the whole of val-08: MEASURED on this checkout
#: (2026-08-28), `pytest autoloop/tests/test_docs_merge.py -q -o
#: cache_dir=/tmp/ptcache` left `git status` byte-identical — no `.pytest_cache`
#: anywhere in the tree — and wrote `/tmp/ptcache/v/cache/nodeids`. So the
#: 2026-08-03 defect is a write-LOCATION problem, and disabling the plugin threw
#: away a feature to solve it.
CACHE_DIR_INI = "cache_dir"

#: What an ADVISORY rerun carries: run only the tests the cache recorded as
#: failing last time. `--lf` rather than `--ff` or `--sw` because it is the flag
#: the one caller that asks for it needs — `AdvisoryValidation`'s confirm step,
#: inside a budget of three runs against a suite that takes minutes — and
#: because `--sw` (stop at the first failure, resume there) does not compose
#: with `-n auto` at all.
#:
#: NEVER ON A VERDICT RUN, and that is structural rather than remembered:
#: `--lf` knows only what failed LAST time and cannot see what a fix newly
#: broke, so it is an advisory instrument and nothing else.
#: `run_validation_commands`' `rerun_last_failed` defaults to False, only
#: `AdvisoryValidation.run` ever passes True, and the injection below sits
#: INSIDE the branch that turned the cache on — so no caller can reach it
#: without also having supplied a `cache_dir`.
RERUN_FAILED_ARGS: tuple[str, ...] = ("--lf",)

#: Every spelling of "use the cache to shrink or reorder this selection". Held
#: as a SET and checked as one because a check spelled `"--lf" not in argv`
#: passes on `--last-failed`, and a guard that misses half the spellings of the
#: thing it forbids is not a guard. Read by `_declares_rerun_selection` (which
#: keeps this module from adding a second one) and by the tests that assert the
#: verdict run carries none of them.
RERUN_SELECTION_FLAGS: frozenset[str] = frozenset(
    {
        "--lf",
        "--last-failed",
        "--ff",
        "--failed-first",
        "--sw",
        "--stepwise",
        "--stepwise-skip",
    }
)

#: A marker expression that SELECTS `isolated` (as opposed to `not isolated`,
#: which every default run carries via `pytest.ini`'s `addopts`). Such a run
#: stays single-process: the marker means "this test needs its own process",
#: and handing it an xdist worker alongside others gives back exactly the
#: company it was marked to avoid. Ambiguous expressions err toward serial.
_SELECTS_ISOLATED_RE = re.compile(r"\bisolated\b")
_DESELECTS_ISOLATED_RE = re.compile(r"\bnot\s+isolated\b")


def failure_digest(
    output: str,
    max_lines: int = _DIGEST_MAX_LINES,
    max_chars: int = _DIGEST_MAX_CHARS,
) -> str:
    """What actually failed, not just how many things did.

    This used to be `splitlines()[-1]` — the LAST line of output. For pytest
    that is the count line ("1 failed, 992 passed"), which discards the
    `FAILED <file>::<test>` lines printed immediately above it. The effect
    was that a refused commit could not be diagnosed from its own blocker
    record: on 2026-08-02 an audit was refused on a single failing test and
    the only way to learn which one was to re-run the tree by hand. A flaky
    gate is bad; a flaky gate that does not say what flaked is worse.

    Keeps the naming lines AND the final count line, since the count is what
    tells you whether one test failed or four hundred did.
    """
    text = _ANSI_RE.sub("", output).strip()
    if not text:
        return "(no output)"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    named = [line for line in lines if _FAILURE_LINE_RE.match(line)]
    picked = named[:max_lines]
    if len(named) > max_lines:
        picked.append(f"(+{len(named) - max_lines} more)")
    # The last line is the count/summary for every runner we use. Include it
    # unless it is already one of the named lines (a run whose only output
    # IS a FAILED line).
    if lines[-1] not in picked:
        picked.append(lines[-1])
    digest = "; ".join(picked)
    if len(digest) > max_chars:
        digest = digest[: max_chars - 3].rstrip() + "..."
    return digest


def _pytest_index(argv: Sequence[str]) -> int | None:
    """Index of the `pytest` token, or None when this is not a pytest run.

    Matched structurally rather than by asking whether "pytest" appears
    anywhere in the argv: a test PATH can contain the word, and `python3 -c`
    programs (`test_validation_env.py` runs one) must never be mistaken for a
    pytest invocation and handed pytest flags.
    """
    if not argv:
        return None
    if Path(argv[0]).name == "pytest":
        return 0
    # Exactly the two interpreter names `SAFE_VALIDATION_BINARIES` allows —
    # deliberately NOT a `python3*` prefix. A versioned basename like
    # `python3.13` (what `sys.executable` gives on many installs) is REFUSED
    # unrun by the allowlist above, so normalizing it here would only decide
    # flags for a command that never launches, and the two rules would drift.
    if Path(argv[0]).name not in ("python", "python3"):
        return None
    # `-m pytest`, wherever it sits (`python3 -X dev -m pytest` is legal).
    # This is the INTERPRETER's `-m`; every `-m` after it is pytest's own
    # marker flag, which is why callers below only ever look past this index.
    for index in range(1, len(argv) - 1):
        if argv[index] == "-m" and Path(argv[index + 1]).name == "pytest":
            return index + 1
    return None


def has_pytest_command(commands: Sequence[Sequence[str]]) -> bool:
    """Does this command list hold a pytest invocation at all?

    The question a caller has to answer before deciding whether a pytest CACHE
    is worth creating: a `ruff`-only list has nowhere to put one and nothing to
    read back from it, so `AdvisoryValidation` makes no directory for one.
    Structural, like the `_pytest_index` it delegates to — the word "pytest"
    inside a test path does not count.
    """
    return any(_pytest_index(tuple(argv)) is not None for argv in commands)


def _declares(args: Sequence[str], flag: str, value: str | None = None) -> bool:
    """Is `flag` already present in `args`, in any spelling pytest accepts?

    Covers separated (`-n auto`, `--numprocesses auto`) and attached (`-nauto`,
    `--numprocesses=auto`) forms. With `value` given, only that exact value
    counts — so `-p no:randomly` does not read as `-p no:cacheprovider`.
    """
    # Every branch below either matches or keeps looking — a non-matching
    # spelling of the same flag (`-p no:randomly` when asked about
    # `-p no:cacheprovider`) must not shadow a real one later in the argv.
    for index, token in enumerate(args):
        if token == flag:
            if value is None or (index + 1 < len(args) and args[index + 1] == value):
                return True
        elif flag.startswith("--") and token.startswith(flag + "="):
            if value is None or token == f"{flag}={value}":
                return True
        elif len(flag) == 2 and len(token) > 2 and token.startswith(flag):
            if value is None or token == flag + value:
                return True
    return False


def _selects_isolated(args: Sequence[str]) -> bool:
    """Does this run ask for the `isolated` marker? (`args` excludes the
    interpreter's own `-m pytest`, so every `-m` here is pytest's.)"""
    for index, token in enumerate(args):
        expression = ""
        if token == "-m" and index + 1 < len(args):
            expression = args[index + 1]
        elif token.startswith("-m") and len(token) > 2:
            expression = token[2:]
        if not expression:
            continue
        if _SELECTS_ISOLATED_RE.search(expression) and not _DESELECTS_ISOLATED_RE.search(
            expression
        ):
            return True
    return False


def _declares_ini(args: Sequence[str], name: str) -> bool:
    """Does `args` already set the pytest ini option `name` on the command line?

    Covers the four spellings pytest accepts for an override — `-o name=v`,
    `-oname=v`, `--override-ini name=v` and `--override-ini=name=v`. Used for
    exactly ONE decision, and a much narrower one than it was: AFTER
    `_without_cache_dir_ini` has removed every `cache_dir` setting that is not
    the caller's own, is the caller's own already there? If it is, this pass has
    nothing to add — which is what makes `effective_validation_command`
    idempotent when a `cache_dir` IS passed.

    It is NOT "does the operator already have a cache_dir, in which case defer to
    it". That was its job until 2026-08-31 and deferring is precisely what an
    advisory run must not do; this module's docstring says why. Nor may a
    REFUSAL rest on it: structural parsing that fails to recognise a spelling
    returns False, and a guard that quietly answers "nothing to see" on the input
    it could not read is not a guard. `_mentions_cache_policy` is the substring
    check that decides fail-closed, and it errs the other way by construction.
    """
    prefix = name + "="
    for index, token in enumerate(args):
        if token in ("-o", "--override-ini"):
            if index + 1 < len(args) and args[index + 1].startswith(prefix):
                return True
        elif token.startswith("--override-ini=") and token[15:].startswith(prefix):
            return True
        elif token.startswith("-o") and token[2:].startswith(prefix):
            return True
    return False


def _declares_rerun_selection(args: Sequence[str]) -> bool:
    """Does `args` already ask for a cache-driven reselection (`--lf`, `--ff`,
    `--sw`, or any of their long spellings)? Checked against the whole set, not
    against `--lf` alone — see `RERUN_SELECTION_FLAGS`."""
    return any(token in RERUN_SELECTION_FLAGS for token in args)


def _without_cache_disabled(args: Sequence[str]) -> tuple[str, ...]:
    """`args` with every `-p no:cacheprovider` removed, in both spellings.

    REPLACEMENT, not deference — and since 2026-08-31 that is the rule for the
    WHOLE cache policy rather than an exception carved out for one flag:
    `_without_cache_dir_ini` does exactly the same to a configured
    `-o cache_dir=`, for the reasons this module's docstring gives. The shipped
    `config.example.toml` spells `-p no:cacheprovider` out on every pytest line
    — `test_the_shipped_list_needs_no_repair_at_run_time` pins that list as a
    fixed point of this module — so a rule that declined to act when the flag
    was already present would pass every synthetic test and be INERT on every
    real deployment, which is the shape of fail-open this repository refuses.

    The two are not settings to reconcile: with the plugin off, `cache_dir` does
    nothing and `--lf` is an unrecognised argument that exits 4 before a test
    runs. Moving the cache OUT of the tree holds the property that flag was
    added for (2026-08-03: a failing run must not dirty the tree it grades), so
    this is the same guarantee by a different mechanism rather than a weakening
    of it — and
    `test_a_failing_validation_run_leaves_the_worker_tree_byte_identical` is the
    check, on the tree itself rather than on a directory name. Its neighbour
    `test_a_configured_in_tree_cache_dir_still_leaves_the_tree_byte_identical`
    runs the same proof against a command that names its own relative cache.

    Reached ONLY when a caller passed a `cache_dir`, and its result may still be
    DISCARDED — the fail-closed arm of `effective_validation_command` throws the
    whole rewrite away and keeps the tokens as written. Every other call leaves
    these tokens exactly where they were.
    """
    kept: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-p" and index + 1 < len(args) and args[index + 1] == "no:cacheprovider":
            index += 2
            continue
        if token == "-pno:cacheprovider":
            index += 1
            continue
        kept.append(token)
        index += 1
    return tuple(kept)


def _without_cache_dir_ini(args: Sequence[str], keep: str) -> tuple[str, ...]:
    """`args` with every `cache_dir` ini override removed EXCEPT `keep`.

    The companion of `_without_cache_disabled`, and there for the same reason: a
    caller that has a validated directory outside the tree is not offering a
    suggestion. A configured `cache_dir` is REMOVED rather than deferred to,
    because both kinds are unusable for an advisory run — a relative one lands in
    the worker tree the gate reads, an absolute one is shared by every task — and
    because pytest takes the LAST `-o cache_dir=` on the line, so a setting left
    in place downstream of the injected one would silently win.

    `keep` is the caller's own `cache_dir=<path>` payload, and keeping it in
    place rather than stripping and re-adding it is what holds idempotence: a
    second pass finds it where the first pass put it, `_declares_ini` sees it,
    and nothing is injected or moved.

    Removal is STRUCTURAL — the four spellings pytest accepts — so it cannot eat
    a token that is not an ini override. The spellings it therefore cannot reach
    (an argparse short cluster like `-qocache_dir=.x`) are not left to chance:
    `_mentions_cache_policy` sees them as substrings and the run falls closed.
    """
    prefix = CACHE_DIR_INI + "="
    kept: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-o", "--override-ini") and index + 1 < len(args):
            if args[index + 1].startswith(prefix) and args[index + 1] != keep:
                index += 2
                continue
        elif token.startswith("--override-ini="):
            setting = token[len("--override-ini=") :]
            if setting.startswith(prefix) and setting != keep:
                index += 1
                continue
        elif token.startswith("-o") and not token.startswith("--"):
            setting = token[2:]
            if setting.startswith(prefix) and setting != keep:
                index += 1
                continue
        kept.append(token)
        index += 1
    return tuple(kept)


def _mentions_cache_policy(args: Sequence[str], allow: str) -> bool:
    """Does anything in `args` still say something about pytest's cache, other
    than the caller's own `allow` setting (`cache_dir=<path>`)?

    THE FAIL-CLOSED TEST, and deliberately a SUBSTRING one rather than a parse.
    Everything else in this module reads argv structurally, which is right when
    the question is "should something be added": a spelling it does not
    recognise costs an extra flag. It is the wrong shape for a REFUSAL, because
    an unrecognised spelling then reads as "nothing there" and the guard turns
    itself off on exactly the input it could not understand.

    Two survivors matter and both are argparse short-option clusters that the
    structural strips above cannot see:

    * a `cache_dir` setting (`-qocache_dir=.x`). It would out-rank the injected
      one, since pytest takes the last `-o cache_dir=` on the line — putting the
      cache wherever the command said, which is the whole defect.
    * a disabled cacheprovider (`-qpno:cacheprovider`). The relocated cache would
      be inert AND `--lf` would be an unrecognised argument: `exit 4` before a
      test runs, which the agent reads as a broken suite and which burns the rest
      of its three-run budget.

    Any command-line `cache_dir` override must carry the literal `cache_dir=` in
    one token — pytest splits an `-o` payload on the first `=` and compares the
    key exactly, so `-o "cache_dir = x"` sets nothing — and any way of switching
    the plugin off must carry `no:cacheprovider`. So the two substrings are
    exhaustive over the spellings that can actually change behaviour, and a
    non-override token that happens to contain one only costs a full run.
    """
    for token in args:
        # The caller's OWN setting, which a second pass sees because the first
        # pass wrote it. Skipped whole rather than tested arm by arm: a temp root
        # whose PATH happened to contain `no:cacheprovider` would otherwise make
        # pass 2 refuse what pass 1 accepted — and refuse it by adding
        # `-p no:cacheprovider` beside the `--lf` pass 1 had already injected,
        # which is `exit 4`. Absurd as a path, fatal as a rule.
        if token == allow:
            continue
        if "no:cacheprovider" in token:
            return True
        at = token.find(CACHE_DIR_INI + "=")
        if at >= 0 and token[at:] != allow:
            return True
    return False


def effective_validation_command(
    argv: Sequence[str],
    *,
    cache_dir: str | Path | None = None,
    rerun_last_failed: bool = False,
) -> tuple[str, ...]:
    """The command that will really run, given the one that was configured.

    A pytest invocation gains `-n auto` (unless it selects the `isolated`
    marker, or already says how many processes it wants — an explicit `-n 0`
    or `-n 4` is an operator decision and is never overridden) and
    `-p no:cacheprovider`. Everything else — `ruff`, `npx vitest`, a bare
    `python3 -c` probe — is returned unchanged.

    Flags are inserted immediately AFTER the `pytest` token rather than
    appended: a command ending in `--` (everything after it is a path, not a
    flag) would otherwise turn `-n auto` into two filenames pytest cannot
    collect.

    **Both keyword arguments default to "no", and with the defaults this
    function is byte-identical to what it was before val-08** — which is what
    keeps the authoritative run, the post-commit re-run and `audit/executor.py`
    unchanged, none of which passes either.

    `cache_dir` moves pytest's cache to that path instead of switching the
    plugin off, and it OVERRIDES whatever the command said about the cache: any
    `-p no:cacheprovider` is removed (`_without_cache_disabled`), any configured
    `cache_dir` is removed (`_without_cache_dir_ini`), and `-o cache_dir=<path>`
    is added in their place. A configured location is not deferred to, because
    neither kind an operator can write is usable here — see this module's
    docstring. If anything the two strips could not parse still MENTIONS a cache
    policy, the rewrite is discarded whole and the command falls back to today's
    behaviour: the tokens exactly as configured, `-p no:cacheprovider`, no
    `--lf`. The same fallback covers a `cache_dir` that is not an ABSOLUTE path:
    pytest resolves a relative one against its rootdir, i.e. into the tree being
    graded, and `""` resolves to that rootdir itself.

    `rerun_last_failed` adds `--lf`. It is honoured ONLY inside the branch that
    turned the cache on, so it cannot produce a command that exits 4 on an
    unrecognised argument, and it is never added on top of a `--lf`/`--ff`/
    `--sw` the caller already wrote.
    """
    argv = tuple(argv)
    start = _pytest_index(argv)
    if start is None:
        return argv
    args = argv[start + 1 :]
    original = args
    # ONE expression for the setting, read by the strip, by the injection and by
    # the fail-closed check. Three spellings of `str(cache_dir)` would agree
    # today and disagree the first time a caller passes a `Path` where the last
    # one passed a string — and the symptom would be a second `-o cache_dir` on
    # the second pass, i.e. an idempotence failure explaining nothing.
    setting = "" if cache_dir is None else f"{CACHE_DIR_INI}={cache_dir}"
    use_cache = False
    # ABSOLUTE OR NOTHING. pytest resolves a relative `cache_dir` against its
    # ROOTDIR — which for a validation run is the tree being graded — so a
    # relative one from a caller is the 2026-08-03 defect with extra steps, and
    # an empty string resolves to the rootdir ITSELF. `AdvisoryValidation` only
    # ever passes an `mkdtemp` result and so can only pass an absolute path; this
    # is the guard for every other caller, and it is string arithmetic, which
    # keeps this function the pure argv rewrite its docstring claims.
    if cache_dir is not None and Path(cache_dir).is_absolute():
        candidate = _without_cache_dir_ini(_without_cache_disabled(args), setting)
        # FAIL CLOSED. The strips are structural and the check is not: whatever
        # survives them is a spelling this cannot rewrite, and the two outcomes
        # of ignoring one are a cache back in the worker tree or a `--lf` beside
        # a disabled plugin. Both are worse than paying for a full run.
        if not _mentions_cache_policy(candidate, setting):
            args = candidate
            use_cache = True
    injected: list[str] = []
    if (
        not _selects_isolated(args)
        and not _declares(args, "-n")
        and not _declares(args, "--numprocesses")
    ):
        injected.extend(PARALLEL_ARGS)
    if use_cache:
        # The only `cache_dir` the strip leaves standing is the caller's own, so
        # finding one here means a previous pass already wrote it — the whole of
        # this function's idempotence under a cache.
        if not _declares_ini(args, CACHE_DIR_INI):
            injected.extend(("-o", setting))
        if rerun_last_failed and not _declares_rerun_selection(args):
            injected.extend(RERUN_FAILED_ARGS)
    elif not _declares(args, "-p", "no:cacheprovider"):
        injected.extend(NO_CACHE_ARGS)
    if not injected and args == original:
        return argv
    return argv[: start + 1] + tuple(injected) + args


def effective_validation_commands(
    commands: Sequence[Sequence[str]],
    *,
    cache_dir: str | Path | None = None,
    rerun_last_failed: bool = False,
) -> tuple[tuple[str, ...], ...]:
    """`effective_validation_command` over a whole configured list.

    This does NOT add pytest commands to a list that had none, and it does not
    change which tests a command selects — `-m isolated` still runs only the
    isolated marker, and a default run still excludes it via `pytest.ini`. It
    changes only HOW a configured pytest command runs.

    The two keyword arguments are passed straight through to every command; see
    `effective_validation_command`. With the defaults, nothing here has moved.
    """
    return tuple(
        effective_validation_command(
            argv, cache_dir=cache_dir, rerun_last_failed=rerun_last_failed
        )
        for argv in commands
    )


def _run_one_command(
    argv: Sequence[str],
    cwd: Path,
    runner,
    timeout: float,
    env,
) -> tuple[bool, str]:
    """`(ok, report_line)` for ONE command.

    Extracted so the loop below can branch on `ok` in one place instead of once
    per failure kind. Every failure mode is still a `False` here rather than an
    exception — a refused binary, a missing binary and a timeout are reported
    exactly like a nonzero exit, which is the promise `run_validation_commands`
    makes to its callers.
    """
    command = " ".join(argv)
    binary = Path(argv[0]).name if argv else ""
    if binary not in SAFE_VALIDATION_BINARIES:
        return False, f"{command}: REFUSED (binary {binary!r} is not a safe validation binary)"
    try:
        proc = runner(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"{command}: TIMEOUT"
    except FileNotFoundError:
        return False, f"{command}: NOT FOUND"
    if proc.returncode == 0:
        return True, f"{command}: PASS"
    output = (proc.stdout or "") + (proc.stderr or "")
    return False, f"{command}: FAIL ({failure_digest(output)})"


def _short_circuit_note(not_run: int) -> str:
    """Why the report has `NOT RUN` lines in it, and how to get a full run.

    Bounded and static — it becomes part of `state.last_validation`, so it is
    under the same discipline as `failure_digest`. It names ONLY a lever that
    exists: `fail_fast=False`. There is deliberately no config key yet
    (`docs/AUTOLOOP.md` §4h), and pointing a reviewer at a setting nothing reads
    would be a false sentence in the one string this change makes honest.

    It ADVISES cheapest-first rather than claiming it. This function sees a
    count, never the command list, and nothing re-orders what an operator
    configured — an expensive-first list is permitted and would simply pay
    expensive-first, so a note asserting the order was cheapest would be false
    on exactly the run that most needs to be told.
    """
    return (
        f"STOPPED at the first failing command: the {not_run} command(s) marked "
        f"{NOT_RUN} above were not executed, which is different information from "
        "PASS. Order is the CONFIGURED order, preserved exactly as written — "
        "nothing here re-orders it, so order the list cheapest-first if a cheap "
        "defect should cost only the cheap check. To run every command anyway — "
        "the 'how much is broken' question rather than 'is this approvable' — "
        "call run_validation_commands(..., fail_fast=False)"
    )


def run_validation_commands(
    commands: Sequence[tuple[str, ...]],
    cwd: Path,
    command_runner=None,
    timeout: float = 1800,
    validation_env: ValidationEnv | None = None,
    fail_fast: bool = True,
    *,
    pytest_cache_dir: str | Path | None = None,
    rerun_last_failed: bool = False,
) -> tuple[bool, str]:
    """Run the commands in `commands` from `cwd`, in order, until one fails.

    Returns `(all_passed, summary)`. `all_passed` is True only if every
    command that ran exited 0; a refused binary, a timeout, or a missing
    binary all count as a failure rather than raising, so a caller can report
    validation failure the same way it reports a nonzero exit. An empty
    `commands` sequence is reported as passed (nothing configured, nothing to
    fail) with a summary saying so — callers that require at least one
    command must check for that themselves.

    **`fail_fast` (default True) stops after the first failing command** — the
    verdict is already decided, and everything after it is paid for against it
    (`docs/AUTOLOOP.md` §4h has the measurements). The commands that did not
    launch are still NAMED, one `NOT RUN` line each, so the summary accounts for
    every configured command and a reviewer can tell "did not run" from
    "passed": different evidence, which collapsing to a single verdict would
    destroy. `fail_fast=False` runs everything. Failure of ANY kind stops the
    run, refusals and timeouts included — they are failures by this function's
    own definition.

    `validation_env`, when given, supplies the database credentials the
    commands run under (see this module's docstring) and redacts its own
    values out of `summary`. When it is None the subprocess environment is
    still built explicitly — the parent environment MINUS the allowlisted
    names — so "no file configured" means "no credentials", never "whatever
    the operator happened to export".

    Every command is normalized through `effective_validation_commands` first —
    including the ones that never launch, so their `NOT RUN` lines name the
    command that WOULD have run — and it is the EFFECTIVE command that runs and
    that the summary names, so the report says what was actually executed rather
    than what was configured a deployment ago. The summary is one
    `PASS`/`FAIL`/`NOT RUN` line per command: parallelism lives inside a
    command, never across the report. A run that passes, and a run whose LAST
    command is the one that fails, are byte-identical to what this returned
    before fail-fast existed — nothing was skipped, so there is nothing to say.

    **`pytest_cache_dir` and `rerun_last_failed` are keyword-only and both
    default to "no"** (val-08). They are handed straight to
    `effective_validation_commands` and nothing else here reads them, so a
    caller that omits them — which is every caller except
    `implement_executor.AdvisoryValidation.run` — gets the same argv, the same
    subprocess and the same summary as before. The flags land in the summary
    text too, because the summary names the EFFECTIVE command: an advisory
    answer therefore SHOWS the agent that its run carried `--lf` rather than
    leaving it to be inferred.
    """
    runner = command_runner or subprocess.run
    env = (
        validation_env.apply()
        if validation_env is not None
        else strip_validation_vars()
    )
    parts: list[str] = []
    all_ok = True
    effective = effective_validation_commands(
        commands, cache_dir=pytest_cache_dir, rerun_last_failed=rerun_last_failed
    )
    for index, argv in enumerate(effective):
        ok, line = _run_one_command(argv, cwd, runner, timeout, env)
        parts.append(line)
        if ok:
            continue
        all_ok = False
        if not fail_fast:
            continue
        remaining = effective[index + 1 :]
        if not remaining:
            break
        parts.extend(f"{' '.join(later)}: {NOT_RUN}" for later in remaining)
        parts.append(_short_circuit_note(len(remaining)))
        break
    if not parts:
        return True, "(no validation commands configured)"
    # Redacted LAST, over the assembled summary, so every branch above is
    # covered by one call rather than each remembering to sanitize itself.
    return all_ok, redact_with(validation_env, "; ".join(parts))


# ---- per-commit test selection ----------------------------------------------
#
# WHY THIS IS AN IMPORT GRAPH AND NOT FILENAME SIMILARITY.
#
# The obvious rule — "a changed file selects the test file with the matching
# name" — was tried in this repository and is disproven by a specific commit.
# On 2026-08-06 auto-01's candidate f06454b5 changed 16 files and updated the
# four test files it touched. All five failures were in
# `autoloop/tests/test_v1_smoke.py`, a file that commit never touched: it
# imports `autoloop.publisher` and `autoloop.orchestrator` directly and
# exercises the publisher and protected-branch push guards the commit changed.
# A name-matching rule runs green there and ships the regression.
#
# Reachability answers the question that actually matters — can this test
# EXECUTE the changed code? — and it answers it in the direction the risk runs:
# from the changed module OUTWARD along the reverse of every import edge, so a
# test file nobody touched is selected whenever it can reach the change through
# any chain of repository modules. `test_v1_smoke.py` is one hop from
# `publisher.py` under that model, which is the property this whole section
# exists to have.
#
# AN IMPORT NAME IS RESOLVED IN THE IMPORTING FILE'S CONTEXT, NOT ONLY AT THE
# REPO ROOT. `autoloop/tests/` has no `__init__.py`, so pytest's prepend import
# mode puts that directory on `sys.path` and its files import each other by BARE
# name — `from test_implement_executor import ...`, which this very repository's
# tests really do, and which `subtitle-scraper/`'s `sys.path.insert` siblings do
# too (CLAUDE.md §10). Indexing every file only by its full dotted path
# (`autoloop.tests.test_implement_executor`) made those names resolve to nothing
# and the edges were dropped silently: a change to an imported test/helper module
# then selected the changed file itself while OMITTING the untouched modules that
# import and execute it — the exact reachability guarantee this section exists to
# provide (found in review, 2026-08-20). So each file's imports are resolved
# against every directory that could plausibly be on `sys.path` when it runs: the
# repo root, plus each of its own ancestor directories that is NOT a package.
# Package directories are excluded because Python 3 has no implicit relative
# imports — inside `autoloop/`, a bare `import publisher` does not find
# `autoloop/publisher.py` — and a relative `from .publisher import ...` is already
# resolved by `_scan_module`.
#
# When a name resolves under more than one of those roots, or to more than one
# file under the same root, EVERY candidate gets an edge. That is what "fail
# conservatively rather than guessing" means in this direction: an extra edge can
# only make more tests run, whereas picking one candidate and being wrong drops a
# test that executes the change.
#
# WHAT IT CANNOT SEE, AND WHAT IS DONE ABOUT IT. A static graph misses coupling
# that is not an import statement: `importlib.import_module`, `__import__`,
# `pytest.importorskip`, a test that spawns a fresh interpreter, and a file that
# does not parse at all. None of those are argued away — every such file is put
# on the frontier UNCONDITIONALLY (`ImportGraph.opaque`), so it is reachable
# from any change and therefore always selected. What select-04 (2026-09-01)
# changed is not that rule but WHICH FILES IT DESCRIBES: a dynamic import whose
# module name is a constant this checkout does not own reaches nothing here,
# and an interpreter name that never reaches a `subprocess` entry point is a
# fixture rather than a spawn. Both are narrowings of ATTRIBUTION; every
# uncertainty — a name that is not constant, an argv that cannot be read, a
# file that will not parse — keeps the frontier answer. It also cannot see a
# non-Python input: a `.toml` a test reads, a fixture, a markdown tracker, an
# `.ini` that decides collection. The import graph models none of those, so
# reachability is not what decides them — the next block is. Every judgement in
# this section resolves the same way — when the answer cannot be established,
# the answer is "run everything", never "assume unrelated".
#
# ONE UNRESOLVABLE PATH MUST NOT VETO THE WHOLE SELECTION (select-01,
# 2026-08-26).
#
# Until this change, ONE changed path the import graph could not resolve
# discarded the graph's answer for every path that DID resolve, and the run went
# full-suite. Measured over the loop's whole transcript on 2026-08-25: selection
# had evaluated 154 rounds and chosen FULL SUITE in 146 of them; on the last day
# every one of the 18 full-suite decisions named this same cause, and the paths
# it named were the documentation trackers (`tasks.TRACKER_PATHS`) — files every
# task is authorized to write and every task updates BY DESIGN. So the
# conservative fallback fired on a path set present in essentially every commit
# the loop makes, and a feature that shipped 2026-08-20 had never once narrowed
# a real round.
#
# The defect was the SCOPE of the fallback, not the fallback. Failing safe on a
# path whose effect cannot be established is right; discarding the answer for
# the paths that DID resolve is not. The selected set is therefore a UNION:
#
#   * tests reachable through the import graph from each changed path that IS a
#     resolvable Python module — unchanged behaviour, and
#   * for each changed path that is NOT, a conservative set attributed to THAT
#     path alone, from repository CONTENT references (`_reference_tokens`,
#     `_files_referencing`): every `.py` file in the checkout whose source names
#     the path, its basename, its stem, its extension or any directory above it,
#     closed over the same reverse-import edges as any other seed, so a file
#     that names the path drags in everything that imports it.
#
# Only when a specific unresolved path can be attributed NOTHING does the run go
# full-suite, and the reason then NAMES that path rather than counting it.
#
# WHY MARKDOWN IS NOT TREATED AS INERT, AND WHY THE RULE IS SAFE ANYWAY. 33 test
# files under `autoloop/tests/` named a documentation tracker before this change
# and 34 do after it, `test_test_selection.py` having joined the population by
# asserting on the seven filenames (measured 2026-08-26 by grep for those names;
# the argument does not depend on the count, and the test that carries it reads
# the population off the checkout rather than hard-coding a number). Skipping
# them on a tracker change would be skipping real coverage —
# `test_markdown_policy.py` exists to assert on
# markdown handling, and `test_docs_merge.py` merges real branches through the
# production note-merge path. This rule skips NONE of them, for a mechanical
# reason rather than a judgement about each file: every one of those files
# contains a `.md` filename literal, so the EXTENSION token alone makes each of
# them a seed for every tracker path. Whether a given file reads the
# repository's own `docs/SUMMARY.md` or builds its own fixture of the same name
# — both shapes are in there — never has to be decided, which is the point: a
# rule that had to tell those apart is a rule that could get one wrong.
#
#   ^ CORRECTED BY select-02 (2026-08-27), and left standing because briefs
#   quote it. The paragraph is accurate about what select-01 shipped and wrong
#   about what this module now does: markdown is still not inert, but "the
#   EXTENSION token alone makes each of them a seed" is no longer true, and the
#   decision it congratulates itself on never having to make — reads the
#   repository's own copy, or builds a fixture of the same name — is exactly
#   the decision the next block makes, because declining to make it selected
#   the whole suite on every round. `test_markdown_policy.py` is now NOT
#   selected by a tracker change: it points `MarkdownPolicy` at a fixture it
#   writes itself, so the tracker's bytes cannot reach it.
#
# WHAT THIS SAVES, STATED SO IT CANNOT BE OVER-READ — AND IT IS SMALL ON EXACTLY
# THE ROUND IT WAS WRITTEN FOR. The brief for this change estimated ~10-15% of
# round wall-clock, on the assumption that the root `tests/` command (the
# language app's suite, which shares no import with the loop) would be dropped
# as unreachable on an autoloop-only round. Measuring the rule before writing it
# says otherwise, and the reason is worth reading before trusting any number
# here: THIS repository's modules document themselves by NAMING their docs.
# `autoloop/__init__.py` names `docs/AUTOLOOP.md` in its module docstring, and
# importing `autoloop.anything` is an edge to it, so every test that imports the
# package at all is attributed to any changed `docs/*.md`. The evaluation
# package's `__init__.py` names `docs/INGESTION_PIPELINE.md` the same way, and
# `lexy-app/backend/tests/conftest.py` names a tracker, which through the
# conftest edge covers that whole tree. So on a tracker-markdown round the
# `autoloop/tests` tree and most of the app's trees are selected nearly whole,
# and what is actually dropped is the TAIL: test files that name nothing and
# import nothing that names anything. Real, bounded, and not a headline.
#
# That is not an argument against the change, because the alternative was not a
# cheaper round — it was FULL SUITE, every time, forever. What this buys is that
# a narrowed round becomes POSSIBLE at all: the resolvable half of every diff is
# now used, and a round whose unresolved paths are LOCALIZED (a JSON fixture, a
# generated reference, a workflow file — anything the repository names in one or
# two places rather than in every module docstring) narrows properly. A rule
# tuned to make the markdown case look good would have to stop following the
# import edge out of a production module that names the path, and that edge is
# the only reason the rule is sound at all.
#
# A PROSE DOCS CHANGE MUST NOT SELECT THE WHOLE SUITE (select-02, 2026-08-27).
#
# select-01 made a narrowed round POSSIBLE. It did not make one HAPPEN, and the
# paragraph above says why without drawing the conclusion: `CLAUDE.md` requires
# every task to append a change note to the documentation trackers, so the
# tracker-markdown round IS the normal round, and on it the token rule
# attributed essentially everything.
#
# TWO SETS OF NUMBERS APPEAR BELOW AND THEY ARE FROM DIFFERENT REPOSITORIES.
# The brief for this task measured the PRE-SPLIT repository — 95 test files
# under `autoloop/tests`, a second root `tests/` tree of 24, 512 files in the
# graph, three configured validation commands — and reported 81/95 and 7/24 for
# `autoloop/validation.py` alone against 95/95 and 22/24 once the change note
# was added, with `docs/TESTS.md` naming 198 of those 512 graph files. NONE of
# that was re-measured here and none of it can be: this checkout was extracted
# with `git filter-repo --path autoloop/` on 2026-08-27, has 93 test files and
# 159 graph files, ships no root `tests/` tree and no `docs/AUTOLOOP.md`, and
# configures two commands. What WAS measured here is the table further down,
# and the one figure that carries the same argument in this checkout is that
# every one of the six trackers attributed 93 of 93 test files under the token
# rule — the whole suite, on the note every task is required to write.
#
# The mechanism is `_reference_tokens`, not the closure. A `.md` path resolves
# to no module, so it falls to the reference-token rule — and its tokens are
# `('.md', 'TESTS', 'TESTS.md', 'docs', 'docs/TESTS.md')`. The bare `docs` and
# `.md` match any source that CITES a document in a docstring, which in a
# repository whose modules document themselves by naming their docs is nearly
# all of them — that is what put the whole suite behind one tracker, before the
# closure ran at all.
#
# So attribution was fixed and the closure was not. For a prose document only:
#
#   * the tokens are dropped for an EXACT match on the path or the basename,
#     plus a glob that matches either — a file that opens a document names it,
#     a file that cites one embeds the name in a sentence, and a sweep
#     (`rglob("*.md")`, `docs/AUDIT_*.md`) names none of them and is kept by
#     the glob arm;
#   * the match is taken off the AST's evaluated string constants, so a
#     comment or a docstring — the mention this whole block is about — does
#     not count; and
#   * the file must be able to address the checkout it lives in (`__file__`).
#     Nothing else in this package can open the changed document: production
#     modules are handed a repo root, and every test that exercises one hands
#     it a fixture. This is the conjunct that stops `tasks.TRACKER_PATHS` —
#     six exact tracker literals in one of the most widely imported modules
#     here — from selecting the suite through the closure.
#
# `.md` IS THE TEST, and `docs/` IS NOT. `docs/audit_charters.toml` is a
# RUNTIME INPUT: the audit parses it and `test_audit_charters.py` asserts the
# shipped bytes equal `DEFAULT_DOMAINS`. It keeps the token rule untouched, as
# does every other unresolvable path. This is one carve-out, not a licence for
# unresolvable paths to select less.
#
# WHAT DOES NOT CHANGE, because failing toward MORE is the whole design: the
# closure still runs on whatever seeds this produces, so a module that reads a
# document still drags in the tests that import it; an unreadable or
# unparseable file is still a seed for every document; and a document nothing
# reads is still attributed NOTHING and still widens the whole run. A prose
# change that narrows is a prose change whose readers were found.
#
# WHAT IT ACTUALLY BOUGHT — the numbers measured HERE, on this tree, by calling
# `select_validation_commands` against it (93 test files, 159 graph files;
# `autoloop/tests` is the only suite, so the brief's second column has no
# counterpart and is not reproduced):
#
#   changed paths                          before      after
#   autoloop/validation.py alone           80/93       80/93
#   + docs/TESTS.md, docs/SUMMARY.md       93/93       80/93
#   val-04-shaped (4 modules + both)       93/93       80/93
#   docs-only (all six trackers)           93/93       72/93
#   docs/audit_charters.toml               93/93       93/93   (unchanged)
#
# The change note now costs the round it rides on NOTHING: every test that
# reads a tracker is already reachable from the module that round changed.
#
# AND THE PART THAT IS NOT A HEADLINE, stated so it cannot be over-read: the
# DOCS-ONLY row is 72, not a handful. `autoloop/dashboard.py` resolves its own
# checkout and holds the bare code literal `"*"` (at least twice, around
# `audit_dir.glob("*")`, scanning an audit-runs directory outside the graph),
# and `"*"` matches any basename, so the glob arm seeds it and 72 tests reach
# it. That is the conservative arm doing exactly what it is for — a sweep names
# no document, and this cannot tell which directory is being swept — and
# tightening it to "the pattern must end in the document's own extension" is
# the obvious next step, deliberately NOT taken here: it would drop a
# `docs_dir.glob("*")` reader silently, which is the failure direction this
# whole section refuses.
#
# THE TWO GAPS THIS RULE HAS, both silent, both bounded, neither closed by the
# empty-attribution backstop — which fires only on an EMPTY set, and each of
# these leaves an INCOMPLETE one:
#
#   * a reader that BUILDS the document's name (`f"{stem}.md"`) rather than
#     spelling it. Asserted absent from this checkout by
#     `test_no_file_that_addresses_this_checkout_builds_a_document_name_dynamically`,
#     which names the file and line of any that appears.
#   * a reader that names the document exactly but resolves the checkout root
#     through ANOTHER module rather than through its own `__file__`. It cannot
#     arise here: `autoloop/tests/conftest.py` exports no root constant (it
#     does `sys.path.insert` and nothing else, by an explicit requirement in
#     its own docstring), and no other module in the package publishes one. If
#     one is ever added, this conjunct is what has to move.
#
# A BOUND THAT USED TO APPLY HERE AND NO LONGER DOES, corrected rather than
# deleted because the old sentence is quoted in briefs:
# `autoloop/tests/conftest.py` imported `autoloop.orchestrator` until brw-16
# (2026-08-25), and a conftest counts as imported by every file beneath it, so a
# change to almost any `autoloop/` module selected the whole `autoloop/tests`
# tree — 92 files, measured the same day. That import is GONE (see that file's
# own docstring, which explains why it must not come back), so an `autoloop/`
# module change no longer selects the tree wholesale. Changing the conftest
# ITSELF still selects everything under it, correctly.

#: The two answers to "which tests does this commit need?", i.e. the accepted
#: values of `[audit] test_selection`.
TEST_SELECTION_REACHABLE = "reachable"
TEST_SELECTION_FULL = "full"
#: Everything `reachable` does, and then WITHIN each selected file the tests that
#: cannot reach the change are deselected (`autoloop.per_test_deps`). Strictly
#: narrower: a test is dropped only when the analysis positively says it reaches
#: nothing changed, so the file set is identical and only the test count moves.
#:
#: DEFAULT OFF. `reachable` narrows to whole FILES and is the mode this
#: repository has run since select-01; the measurements that justify this one are
#: in the commit that added `per_test_deps`, and the check that it never drops a
#: test which really does exercise the change is a CI job
#: (`scripts/check_selection_soundness.py`), not an argument.
TEST_SELECTION_PER_TEST = "per_test"
TEST_SELECTION_MODES: tuple[str, ...] = (
    TEST_SELECTION_REACHABLE,
    TEST_SELECTION_FULL,
    TEST_SELECTION_PER_TEST,
)
#: The modes that narrow at all. `full` is the only one that does not.
_NARROWING_MODES = frozenset({TEST_SELECTION_REACHABLE, TEST_SELECTION_PER_TEST})

#: Directory names the graph walk never descends into. Dot-prefixed directories
#: are skipped as a class (`.git`, `.venv`, `.pytest_cache`, `.autoloop`); this
#: names the rest.
_GRAPH_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "env",
        "site-packages",
        "build",
        "dist",
    }
)

#: Above this many Python files the walk gives up and the run widens to the
#: full suite. A bound on the WORK, not a judgement about the repository: this
#: parses every file with `ast` on every validated commit, and a checkout that
#: large is one where the parse itself would cost more than the tests it saves.
#: This repo is ~430 Python files, so the bound leaves an order of magnitude of
#: headroom.
_GRAPH_MAX_FILES = 6000

#: Function names whose presence means "this file's imports cannot be read from
#: its import statements". Matched on the called name only (`import_module`,
#: not `importlib.import_module`) so an aliased import cannot hide one.
_DYNAMIC_IMPORT_CALLS = frozenset(
    {
        "__import__",
        "import_module",
        "importorskip",
        "load_module",
        "exec_module",
        "module_from_spec",
        "spec_from_file_location",
        "run_module",
        "run_path",
    }
)

#: The subset of the above whose FIRST POSITIONAL ARGUMENT is a module NAME, so
#: a string constant there says exactly which module is being reached for. Those
#: are the only ones select-04 (2026-09-01) resolves: a constant naming a module
#: this repository does not own — `__import__("socket")`,
#: `pytest.importorskip("asyncpg")` — cannot reach repository code by any route,
#: so it is not evidence of hidden coupling.
#:
#: The five left out take something else: `spec_from_file_location(name, path)`
#: imports from the PATH, so its name argument proves nothing about what is
#: loaded; `run_path` takes a path; `exec_module`/`module_from_spec`/
#: `load_module` take an already-built module or spec. Each of those stays
#: opaque unconditionally, exactly as before.
_NAMED_DYNAMIC_IMPORTS = frozenset(
    {"__import__", "import_module", "importorskip", "run_module"}
)

#: The `subprocess` entry points that actually start a process. An interpreter
#: reference is counted as evidence of a spawn only when it reaches one of
#: these — see `_scan_module`.
#:
#: Matched against the names this file BOUND (`import subprocess as sp`,
#: `from subprocess import Popen`) rather than by name alone, because `run` is
#: also what `orch.run(max_steps=1)` is called and those are everywhere in this
#: suite. That is the opposite choice from `_DYNAMIC_IMPORT_CALLS`, and for the
#: opposite reason: there, matching wide costs an extra opaque file; here it
#: would cost the narrowing entirely.
#:
#: WHAT THIS DOES NOT WATCH, stated because the previous rule covered it by
#: accident: `os.execv`, `os.system`, `os.posix_spawn` and their relatives all
#: start a process without touching `subprocess`. No file in this checkout uses
#: one, and `test_test_selection.py::
#: test_no_file_left_off_the_frontier_reaches_an_interpreter_around_subprocess`
#: fails the round that introduces one rather than leaving it to be noticed.
_SUBPROCESS_CALLS = frozenset({"run", "Popen", "check_output", "check_call"})

#: String constants that mean "this file may launch a fresh interpreter", which
#: can import anything in the repository without a single import statement.
#: `sys.executable` is detected separately, as an attribute.
#:
#: Only consulted for TEST files (see `_scan_module`). Half the modules in
#: this package mention an interpreter name because running one is their job —
#: `run_validation_commands` two hundred lines above is the clearest case — and
#: treating those as opaque would put a production module on every change's
#: frontier, dragging in everything that imports it. What the signal is actually
#: about is a TEST that exercises repository code through a subprocess instead
#: of an import, which is the coupling the graph cannot see.
#:
#: NARROWED ONE STEP FURTHER by select-04 (2026-09-01), from file KIND to
#: FLOW. The sentence above is an argument about what the signal is for, and a
#: test file that mentions an interpreter without ever launching one does not
#: carry it: `test_validation_parallelism.py`'s `LEGACY_SERIAL` is a tuple
#: describing what an operator's config.toml contains, and this repository —
#: whose subject IS running validation commands — has that vocabulary
#: everywhere. So the literal is counted only when it reaches a `subprocess`
#: entry point (`_SUBPROCESS_CALLS`). It is NOT permission to narrow anything
#: else: a spawn whose argv this cannot read is counted exactly as if it held
#: an interpreter, which is what `_spawn_argv_is_inert` returning `False`
#: means.
_INTERPRETER_LITERALS = frozenset({"python", "python3"})

#: How many entries the evidence line names before it counts the rest. The
#: string it builds becomes `state.last_validation` — state.json, the
#: transcript, blocker records and the CONTEXT block of every message — so it is
#: bounded for the same reason `failure_digest` and `packet._format_assumptions`
#: are.
_EVIDENCE_MAX_SELECTED = 12
_EVIDENCE_MAX_CONSIDERED = 8

#: The sentence that keeps a narrowed round from reading as LESS validation
#: than the round before it, which is the refusal this whole feature has to
#: survive (hlth-01 and dash-04 each burned five rounds on "the evidence is too
#: narrow", 2026-08-15/16).
#:
#: It is a claim about a DIFFERENT module, so it is stated as the fact it is
#: rather than as an argument, and it is pinned by a test:
#: `test_test_selection.py::test_the_pre_commit_run_is_narrowed_too` drives a
#: real `ImplementExecutor` round and asserts the configured pytest command ran
#: NARROWED and that the round's own summary says so. **When either call site's
#: behaviour changes, this sentence has to change with it — that test is the
#: alarm.**
#:
#: REWRITTEN by val-04 (2026-08-27), which narrowed the pre-commit run too. The
#: sentence it replaced said the recorded subset was "ADDED to a full-suite run
#: of the same tree", which stopped being true the moment that call site
#: narrowed; the version below states the consequence — a full-suite run is no
#: longer guaranteed at either phase — instead of leaving a reviewer to infer
#: it. It is deliberately a claim about the MODEL both phases use rather than
#: about what the other phase decided this round: this function is called by
#: each phase separately and cannot see the other's answer, so a sentence
#: asserting "the other run also narrowed" would be unverifiable from here.
#:
#: BUDGETED, not just written: `evidence()` is bounded to under 3,000
#: characters by `test_the_evidence_is_bounded`, and everything around this
#: sentence is fixed, so this constant has roughly 690 characters of room and
#: the first draft of the val-04 rewrite (830) failed that test. The facts it
#: had to keep are the four below; the elaboration went to this module's own
#: docstring, which no message carries.
PRECOMMIT_EVIDENCE = (
    "Scope of the narrowing: BOTH runs of this round, since 2026-08-27. The "
    "authoritative PRE-COMMIT run (`implement_executor.py`) selects through "
    "this same function, over the same command list, from its own changed "
    "paths. So this is NOT a subset added to a full-suite run of the same "
    "tree — no full-suite run is guaranteed at either phase, and the "
    "post-commit re-run, though it does re-run the COMMITTED tree a hook can "
    "have changed, is no longer an independent backstop under it. Each phase "
    "reports its own decision where it ran."
)

#: pytest flags that consume the NEXT argv token. A token following one of
#: these is a value, never a test path — `-m isolated` must not select a file
#: called `isolated`.
_PYTEST_VALUE_FLAGS = frozenset(
    {
        "-c",
        "-k",
        "-m",
        "-n",
        "-o",
        "-p",
        "-r",
        "-W",
        "--numprocesses",
        "--maxfail",
        "--rootdir",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--dist",
        "--junitxml",
        "--tb",
        "--durations",
        "--log-file",
        "--override-ini",
    }
)

#: pytest flags that consume nothing. Anything starting with `-` that is in
#: NEITHER set (and is not a `--flag=value`) is UNKNOWN, and an unknown flag
#: makes this refuse to rewrite that command at all rather than guess which of
#: its tokens are paths.
_PYTEST_BOOL_FLAGS = frozenset(
    {
        "-q",
        "-qq",
        "-v",
        "-vv",
        "-s",
        "-x",
        "-l",
        "-ra",
        "-rA",
        "--quiet",
        "--verbose",
        "--exitfirst",
        "--collect-only",
        "--co",
        "--no-header",
        "--no-summary",
        "--strict-markers",
        "--strict-config",
        "--showlocals",
        "--full-trace",
        "--disable-warnings",
    }
)


def _is_test_file(rel: str) -> bool:
    """pytest's own default discovery rule, and only that.

    Deliberately not "lives under a directory called tests": a helper module
    sitting beside the tests is a graph NODE (things import it) but is not
    something to hand pytest as a path.
    """
    name = PurePosixPath(rel).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _module_parts(rel: str) -> tuple[str, ...]:
    """The dotted module a repo-relative path would be imported as."""
    parts = PurePosixPath(rel).parts
    if parts[-1] == "__init__.py":
        return tuple(parts[:-1])
    return tuple(parts[:-1]) + (parts[-1][:-3],)


def _package_dirs(files: Sequence[str]) -> frozenset[str]:
    """Every directory in the checkout that holds an `__init__.py`.

    The repo root itself can be one (`""`), and that is fine: it is added to
    `_import_roots` unconditionally, since it is on `sys.path` for any
    `python3 -m pytest` run started there.
    """
    dirs: set[str] = set()
    for rel in files:
        if PurePosixPath(rel).name == "__init__.py":
            parent = PurePosixPath(rel).parent.as_posix()
            dirs.add("" if parent == "." else parent)
    return frozenset(dirs)


def _import_roots(rel: str, packages: frozenset[str]) -> tuple[str, ...]:
    """Directories an ABSOLUTE import inside `rel` could resolve against.

    The repo root, plus EVERY ancestor directory of `rel` that is not a package.

    That is deliberately WIDER than Python's own rule, which would stop at the
    first non-package ancestor (the directory pytest's prepend import mode
    inserts, and the one `subtitle-scraper/`'s `sys.path.insert` idiom inserts).
    Stopping there is not enough for this repository: the backend suite runs with
    a cwd of `lexy-app/backend` (`docs/TESTS.md`), so its tests' bare
    `from services import ...` resolves under a directory that is NOT their
    prepend root, and the first-ancestor rule would drop those edges — the same
    class of silent drop this whole resolution change exists to fix. Taking every
    non-package ancestor covers "whatever directory this suite is actually
    launched from" without having to model cwd per command. The cost is extra
    aliases in deep trees, which can only ADD edges, i.e. run more tests.

    Package directories are excluded because Python 3 has no implicit relative
    imports, so a bare name inside one does not reach its sibling — the sibling
    is reached by `from .x import ...`, which `_scan_module` already
    resolves. That exclusion is a precision choice, not a guarantee: nothing
    depends on an edge being ABSENT, and a directory that gains an `__init__.py`
    while still being imported from by bare name would be a reason to widen this
    rule rather than to trust it.

    Sorted, so the graph does not depend on iteration order.
    """
    parts = list(PurePosixPath(rel).parts[:-1])
    roots = {""}
    while parts:
        candidate = "/".join(parts)
        if candidate not in packages:
            roots.add(candidate)
        parts.pop()
    return tuple(sorted(roots))


def _python_files(root: Path) -> tuple[str, ...]:
    """Every `.py` file in the checkout, repo-relative, sorted.

    Sorted at every level (`os.walk`'s `dirnames` too) so the whole graph — and
    therefore the selection built from it — is a pure function of the tree, not
    of filesystem iteration order. Symlinked directories are not followed:
    `os.walk` does not by default, and a symlink out of the checkout would put
    files this cannot map to repo-relative paths into the graph.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _GRAPH_SKIP_DIRS and not name.startswith(".")
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(Path(dirpath, name).relative_to(root).as_posix())
                if len(found) > _GRAPH_MAX_FILES:
                    return tuple(sorted(found))
    return tuple(sorted(found))


def _names_an_interpreter(value: object) -> bool:
    """Is this constant an interpreter, or a shell string that starts one?

    Whole-token, never substring: `"python3"` and `"python3 -m pytest"` both
    name one, `"/usr/bin/python3"` and `"pythonic"` do not. That is the same
    vocabulary `_INTERPRETER_LITERALS` has always used — widening it is a
    separate question, and widening it HERE while the module-level scan stayed
    narrow would make the two halves disagree.
    """
    if not isinstance(value, str):
        return False
    return value in _INTERPRETER_LITERALS or bool(
        _INTERPRETER_LITERALS & set(value.split())
    )


#: `_spawn_argv_verdict`'s three answers.
_SPAWN_INERT = "inert"
_SPAWN_INTERPRETER = "interpreter"
_SPAWN_UNREADABLE = "unreadable"


def _spawn_argv_verdict(call: ast.Call) -> str:
    """What program does this `subprocess` call start?

    `_SPAWN_INERT` only when the program is spelled out: the argv is a
    list/tuple of constants (or one constant, for `shell=True`) and not one of
    them names an interpreter. `subprocess.run(["git", "status"])` qualifies.
    `_SPAWN_INTERPRETER` when it is spelled out AND one of them does.

    EVERYTHING ELSE IS `_SPAWN_UNREADABLE`, which is where this fails closed.
    `subprocess.run(["git", *args])` inside a `run_git(cwd, *args)` helper does
    not qualify — `args` is a parameter and a caller could pass anything — and
    neither does `subprocess.run(cmd)`, `subprocess.run(build_argv())`, or a
    spawn with no argv argument at all. A file that mentions an interpreter AND
    spawns something this cannot read is exactly the "the flow cannot be
    established" case, and it keeps today's answer.
    """
    argv = None
    if call.args:
        argv = call.args[0]
    else:
        for keyword in call.keywords:
            if keyword.arg == "args":
                argv = keyword.value
                break
    if argv is None:
        return _SPAWN_UNREADABLE
    elements = argv.elts if isinstance(argv, (ast.List, ast.Tuple)) else [argv]
    verdict = _SPAWN_INERT
    for element in elements:
        if not isinstance(element, ast.Constant):
            return _SPAWN_UNREADABLE
        if _names_an_interpreter(element.value):
            verdict = _SPAWN_INTERPRETER
    return verdict


def _dynamic_import_leaves_the_repository(
    call: ast.Call, called: str, owns_module
) -> bool:
    """Does this dynamic import demonstrably name a module this repo does not own?

    Every `False` keeps the file opaque, and there are five separate ways to
    get one: no resolver was supplied, the call is not one whose first
    positional argument is a module NAME (`_NAMED_DYNAMIC_IMPORTS`), it has no
    positional argument, that argument is not a string constant
    (`import_module(name)`, `import_module(*parts)`, `import_module(f"{p}.x")`),
    or the name is relative and so means nothing without the caller's package.
    Only a constant, absolute, non-repository name narrows anything.
    """
    if owns_module is None or called not in _NAMED_DYNAMIC_IMPORTS:
        return False
    if not call.args:
        return False
    first = call.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return False
    name = first.value
    if not name or name.startswith("."):
        return False
    return not owns_module(name)


def _attribute_base(node: ast.expr) -> str:
    """The last segment of `node`'s own qualifier, or `""`.

    `subprocess.run` -> `subprocess`; `orchestrator_module.subprocess.run` ->
    `subprocess`; `orch.run` -> `orch`.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _scan_module(tree: ast.AST, rel: str, owns_module=None) -> tuple[bool, set[str]]:
    """Both questions `build_import_graph` asks of a parsed file, in ONE walk.

    These were two functions — `_file_is_opaque` and `_imported_modules` — until
    2026-08-28, and each walked the whole tree by itself. `ast.walk` is a
    Python-level traversal (a deque plus `iter_child_nodes` plus `iter_fields`
    per node), so the pair was the single largest cost in a selection: measured
    on this checkout's 159 files, 0.98s for the two passes against 0.63s for the
    one below, inside a `select_validation_commands` call where `ast.walk`
    accounted for 63% of the time. WHICH nodes count did not change; the two
    branches below are the former functions' bodies, and their agreement was
    checked file-by-file over the whole checkout before the split was removed.

    Returns `(opaque, imported)`.

    `opaque` — can this file reach repository code by some route other than its
    own import statements? A `True` here is not a defect report: it puts the
    file on the frontier for EVERY change, which is the conservative answer. A
    dynamic import counts anywhere; an interpreter subprocess counts only in a
    test file or a conftest, for the reason `_INTERPRETER_LITERALS` gives. It no
    longer returns the moment it knows — the import half has to finish the walk
    regardless — so once the answer is `True` the remaining nodes only skip the
    opacity tests, which is what the `elif opaque` arm is for.

    `owns_module` decides the dynamic-import half: a predicate answering "is
    this dotted name a module in this checkout?", supplied by
    `build_import_graph`, which is the only place that knows. **Omitting it
    keeps every dynamic import opaque**, which is what any caller without a
    file map has to get.

    TWO NARROWINGS, both select-04 (2026-09-01), both of ATTRIBUTION rather
    than of the conservative default:

    * a dynamic import whose module name is a CONSTANT this repository does not
      own (`__import__("socket")`, `pytest.importorskip("asyncpg")`) is not
      evidence of hidden coupling — see `_dynamic_import_leaves_the_repository`
      for the five ways that check declines to narrow;
    * an interpreter literal or `sys.executable` counts only once a `subprocess`
      entry point actually starts something this cannot read as an
      interpreter-free constant argv — see `_spawn_argv_verdict`.

    The second is why the walk can no longer stop at the first interpreter
    literal: which `subprocess` names this file bound is not known until the
    imports have all been seen, so the flags are collected and combined at the
    end. `opaque` short-circuits on a dynamic import only.

    `imported` — every dotted module name this file names in an import
    statement. Relative imports are resolved against the file's own package, so
    `from .validation import x` inside `autoloop/orchestrator.py` yields
    `autoloop.validation`. `from a import b` yields BOTH `a` and `a.b`, because
    `b` may be a submodule rather than an attribute and only the file map
    downstream can tell which.
    """
    watch_interpreter = _is_test_file(rel) or PurePosixPath(rel).name == "conftest.py"
    parts = _module_parts(rel)
    package = list(parts) if rel.endswith("__init__.py") else list(parts[:-1])
    opaque = False
    names: set[str] = set()
    interpreter_seen = False
    # `subprocess` under whatever name this file imported it as, plus any entry
    # point pulled out of it by `from subprocess import ...`. Read off the
    # imports rather than assumed, so `import subprocess as sp` cannot hide a
    # spawn — and so `orch.run(max_steps=1)`, which shares a name with
    # `subprocess.run` and nothing else, is not mistaken for one.
    subprocess_aliases: set[str] = {"subprocess"}
    bare_spawns: set[str] = set()
    # Attributes named like a spawn, and the ids of the ones that are being
    # CALLED. What is left over is a reference that escapes — `runner =
    # subprocess.run` — which goes somewhere this cannot follow.
    spawn_attributes: list[ast.Attribute] = []
    spawn_callees: set[int] = set()
    spawn_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                if alias.name == "subprocess" and alias.asname:
                    subprocess_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `level` counts dots: 1 = this package, 2 = its parent.
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue
                base_parts = package[:keep]
            else:
                base_parts = []
            if node.module:
                base_parts = base_parts + node.module.split(".")
            base = ".".join(base_parts)
            if base:
                names.add(base)
            for alias in node.names:
                if alias.name != "*":
                    names.add(".".join(filter(None, [base, alias.name])))
            if node.module == "subprocess" and not node.level:
                for alias in node.names:
                    if alias.name == "*":
                        bare_spawns |= _SUBPROCESS_CALLS
                    elif alias.name in _SUBPROCESS_CALLS:
                        bare_spawns.add(alias.asname or alias.name)
        elif opaque:
            continue
        elif isinstance(node, ast.Call):
            func = node.func
            called = ""
            if isinstance(func, ast.Attribute):
                called = func.attr
            elif isinstance(func, ast.Name):
                called = func.id
            if called in _DYNAMIC_IMPORT_CALLS and not _dynamic_import_leaves_the_repository(
                node, called, owns_module
            ):
                opaque = True
            elif watch_interpreter and called in _SUBPROCESS_CALLS:
                spawn_calls.append(node)
                if isinstance(func, ast.Attribute):
                    spawn_callees.add(id(func))
        elif watch_interpreter and isinstance(node, ast.Attribute):
            if (
                node.attr == "executable"
                and isinstance(node.value, ast.Name)
                and node.value.id == "sys"
            ):
                interpreter_seen = True
            elif node.attr in _SUBPROCESS_CALLS:
                spawn_attributes.append(node)
        elif watch_interpreter and isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value in _INTERPRETER_LITERALS:
                interpreter_seen = True
    if watch_interpreter and not opaque:
        starts_an_interpreter = False
        unreadable = any(
            id(attribute) not in spawn_callees
            and _attribute_base(attribute.value) in subprocess_aliases
            for attribute in spawn_attributes
        )
        for call in spawn_calls:
            func = call.func
            if isinstance(func, ast.Attribute):
                if _attribute_base(func.value) not in subprocess_aliases:
                    continue
            elif func.id not in bare_spawns:
                continue
            verdict = _spawn_argv_verdict(call)
            starts_an_interpreter |= verdict == _SPAWN_INTERPRETER
            unreadable |= verdict == _SPAWN_UNREADABLE
        opaque = starts_an_interpreter or (interpreter_seen and unreadable)
    return opaque, names


@dataclass(frozen=True)
class ImportGraph:
    """Who imports whom, in the direction risk travels.

    `importers[path]` is every repo file that imports `path` — the REVERSE of
    the import statements, because the question being asked is "what could
    execute this?" rather than "what does this need?".
    """

    files: frozenset[str]
    importers: dict[str, frozenset[str]]
    #: Files whose own imports could not be read (dynamic import, interpreter
    #: subprocess, parse error). Seeded onto the frontier for every change.
    opaque: frozenset[str]
    #: `True` when the walk hit `_GRAPH_MAX_FILES` and stopped.
    truncated: bool = False

    @property
    def test_files(self) -> frozenset[str]:
        return frozenset(path for path in self.files if _is_test_file(path))

    def reachable_from(self, seeds: Iterable[str]) -> frozenset[str]:
        """Transitive closure over `importers`, starting from `seeds` plus every
        opaque file. Iterative rather than recursive — an import cycle is normal
        in a real package and must not blow the stack."""
        frontier = [path for path in sorted(set(seeds) | set(self.opaque)) if path in self.files]
        seen = set(frontier)
        while frontier:
            current = frontier.pop()
            for importer in self.importers.get(current, frozenset()):
                if importer not in seen:
                    seen.add(importer)
                    frontier.append(importer)
        return frozenset(seen)


def build_import_graph(root: Path) -> ImportGraph:
    """Parse every `.py` file under `root` and build the reverse import graph.

    Two edge kinds beyond the literal import statement, both conservative:

    * **Package `__init__.py`.** Importing `a.b.c` executes `a/__init__.py` and
      `a/b/__init__.py`, so an edge is added to each existing ancestor package.
      Without it a change to a package's `__init__` would reach nothing.
    * **`conftest.py`.** pytest applies a conftest to its whole directory tree
      without any file importing it. Every `.py` file under a conftest's
      directory therefore gets an edge FROM that conftest, so changing one
      selects the tests it configures.

    A file that cannot be parsed is recorded as opaque rather than skipped: its
    imports are unknown, so it must be treated as importing everything.

    An import NAME is looked up once per `_import_roots` entry for the importing
    file — the repo root and its non-package ancestors — and every file the name
    matches, under any of them, gets an edge. See the "an import name is resolved
    in the importing file's context" note at the top of this section for why the
    repo-root-only lookup that preceded this dropped real edges, and why the
    ambiguous case unions rather than picks.
    """
    files = _python_files(root)
    truncated = len(files) > _GRAPH_MAX_FILES
    if truncated:
        return ImportGraph(frozenset(files), {}, frozenset(files), truncated=True)
    packages = _package_dirs(files)
    parts_of = {rel: _module_parts(rel) for rel in files}
    roots_of = {rel: _import_roots(rel, packages) for rel in files}
    # `by_root[prefix][dotted]` is every file importable as `dotted` when
    # `prefix` is the `sys.path` entry. A SET, not a single path: two files can
    # claim one dotted name (`x.py` beside `x/__init__.py`), and the previous
    # single-valued map resolved that collision by last-write-wins — silently
    # dropping one of the two files' importers.
    by_root: dict[str, dict[str, set[str]]] = {}
    for prefixes in roots_of.values():
        for prefix in prefixes:
            by_root.setdefault(prefix, {})
    for prefix, table in by_root.items():
        depth_of_prefix = len(prefix.split("/")) if prefix else 0
        for rel in files:
            if prefix and not rel.startswith(prefix + "/"):
                continue
            table.setdefault(".".join(parts_of[rel][depth_of_prefix:]), set()).add(rel)

    def owner_of(rel: str):
        """Does this checkout own a given module name, answered in `rel`'s own
        import context — the same `by_root`/`roots_of` lookup the edge loop
        below performs, so a dynamic import and a written one resolve
        identically. A name matching nothing under any of `rel`'s roots is
        stdlib, third-party, or nothing at all."""

        def owns(name: str) -> bool:
            segments = name.split(".")
            for prefix in roots_of[rel]:
                table = by_root[prefix]
                for depth in range(1, len(segments) + 1):
                    if table.get(".".join(segments[:depth])):
                        return True
            return False

        return owns

    importers: dict[str, set[str]] = {}
    opaque: set[str] = set()
    for rel in files:
        try:
            source = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            opaque.add(rel)
            continue
        file_is_opaque, modules = _scan_module(tree, rel, owner_of(rel))
        if file_is_opaque:
            opaque.add(rel)
        for module in modules:
            segments = module.split(".")
            for prefix in roots_of[rel]:
                table = by_root[prefix]
                for depth in range(1, len(segments) + 1):
                    for target in table.get(".".join(segments[:depth]), ()):
                        if target != rel:
                            importers.setdefault(target, set()).add(rel)
    for rel in files:
        if PurePosixPath(rel).name != "conftest.py":
            continue
        prefix = PurePosixPath(rel).parent.as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        for other in files:
            if other != rel and other.startswith(prefix):
                importers.setdefault(rel, set()).add(other)
    return ImportGraph(
        files=frozenset(files),
        importers={key: frozenset(value) for key, value in importers.items()},
        opaque=frozenset(opaque),
    )


def _reference_tokens(rel: str) -> tuple[str, ...]:
    """Every literal a repository file would have to contain to NAME `rel`.

    The path itself, its basename, its stem, its extension, and every ancestor
    directory in both spellings (full prefix and bare name). A file that reads
    `rel` has to say at least one of them somewhere: `open("docs/SUMMARY.md")`
    says the first, `docs / "SUMMARY.md"` the second and the ancestor,
    `docs / f"{name}.md"` the ancestor and the extension, and a directory sweep
    like `root.rglob("*.md")` says the extension.

    Deliberately over-broad and deliberately UN-TUNED: no minimum token length,
    no word-boundary requirement, and no attempt to tell a docstring mention
    from a real `open()`. Every one of those refinements would make the seed set
    SMALLER, which is the direction that drops a test which executes the change;
    a token that matches too much only makes more tests run. A one-character
    directory name matching most of the checkout is the acceptable failure here,
    and it degrades to today's behaviour (run everything) rather than to a
    silent gap.

    Sorted, so the seed set — and therefore the selection built from it — does
    not depend on set iteration order.

    The ancestor walk has TWO stops and the second is the load-bearing one: the
    familiar roots, and a parent that is not SHORTER than the one before it.
    `PurePosixPath("//x").parent` is `//`, whose own parent is `//` again —
    POSIX leaves a leading double slash implementation-defined and pathlib
    preserves it — so the root list alone spins forever on a shape git would
    never report and nothing here validates. A selector that hangs is worse
    than one that widens.
    """
    path = PurePosixPath(rel)
    tokens = {rel, path.name, path.stem, path.suffix}
    parent = path.parent
    while parent.as_posix() not in (".", "/", ""):
        tokens.add(parent.as_posix())
        tokens.add(parent.name)
        nxt = parent.parent
        if len(nxt.as_posix()) >= len(parent.as_posix()):
            break
        parent = nxt
    return tuple(sorted(token for token in tokens if token))


def _files_referencing(
    root: Path,
    files: Sequence[str],
    tokens_by_path: dict[str, tuple[str, ...]],
) -> dict[str, frozenset[str]]:
    """`{changed path: every graph file whose SOURCE names it}`.

    One read per file for the WHOLE batch rather than one pass per changed path,
    because the caller has just parsed the same files to build the graph and a
    second full walk per path would multiply that cost by the size of the diff.

    **A file this cannot read is a seed for EVERY path, not for none.** That is
    the whole failure mode this function could have: a scan that quietly drops
    its unreadable input is a check that silently passes, and the alarm never
    fires. `build_import_graph` usually catches the same file on the same
    `read_text` and marks it `opaque`, which `reachable_from` unions into every
    seed set — but "usually" is not an argument: a file that parsed during the
    walk and became unreadable a moment later is in neither set, and relying on
    the graph to have covered it would make correctness here depend on a race.
    Adding it can only run more tests. `ValueError` is in the tuple deliberately:
    `UnicodeDecodeError` (a binary file named `.py`) is a `ValueError`, not an
    `OSError`, and letting it propagate would abort a selection rather than
    widen it.
    """
    hits: dict[str, set[str]] = {path: set() for path in tokens_by_path}
    for rel in files:
        try:
            source = (root / rel).read_text(encoding="utf-8")
        except (OSError, ValueError):
            for found in hits.values():
                found.add(rel)
            continue
        for path, tokens in tokens_by_path.items():
            if any(token in source for token in tokens):
                hits[path].add(rel)
    return {path: frozenset(found) for path, found in hits.items()}


#: The extension that makes a changed path PROSE DOCUMENTATION rather than a
#: machine-read input. Extension alone is enough to tell the two apart in this
#: repository and the distinction is checked, not assumed: `docs/` holds
#: `audit_charters.toml`, which `audit/executor.py` PARSES (config
#: `[repo].audit_charters_file`) and whose shipped bytes
#: `test_audit_charters.py` asserts against `DEFAULT_DOMAINS` — a change there
#: really does change behaviour, and it keeps the general
#: `_reference_tokens` treatment because it is not a `.md`. Living under
#: `docs/` is NOT the test, and neither is being one of
#: `tasks.TRACKER_PATHS`: `CLAUDE.md` at the root is prose too, and a rule
#: keyed on a list of tracker paths would silently treat the next prose file
#: added beside them as machine input.
_PROSE_DOC_SUFFIX = ".md"


def _is_prose_document(rel: str) -> bool:
    """Is `rel` a document written for a HUMAN reader?

    Case-sensitive, and that is the safe direction: a `README.MD` is not
    recognised, falls to `_reference_tokens`, and therefore selects MORE.
    """
    return PurePosixPath(rel).suffix == _PROSE_DOC_SUFFIX


def _code_strings(tree: ast.AST) -> set[str]:
    """Every string constant the module EVALUATES, minus its prose.

    Comments never reach the AST at all, so they need no handling. A docstring
    — module, class, function, or a free-floating string block between
    statements — is an `Expr` whose value is the constant, and is dropped: that
    is the file TALKING ABOUT a document, which is exactly the mention this
    scan must stop counting as a dependency.

    An f-string is `JoinedStr` and its literal pieces are `Constant`s under it,
    so `f"docs/{name}.md"` contributes `"docs/"` and `".md"` and matches no
    document exactly. That is a known gap, not an oversight — see
    `_files_reading_documents`.
    """
    # ONE walk, not two. An `Expr` node is never itself a `Constant`, so the
    # two arms cannot both fire for a node, and the prose set is applied after
    # the walk rather than during it — a docstring's constant is reached as a
    # child of its `Expr` in the same pass, in whichever order `ast.walk`
    # happens to visit them.
    prose: set[int] = set()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            prose.add(id(node.value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((id(node), node.value))
    return {value for node_id, value in found if node_id not in prose}


def _addresses_own_checkout(tree: ast.AST) -> bool:
    """Can this module reach the checkout it is PART OF, unaided?

    `__file__` is the only way a file in this repository resolves its own
    checkout root; every production module is handed a repo root instead
    (`GitGateway(repo, ...)`, `MarkdownPolicy(repo)`, `load_charter_domains(repo,
    ...)`), and every test that exercises one points it at a fixture it wrote
    itself. So a module WITHOUT `__file__` cannot open the changed document —
    it can only hold its path, which is what `tasks.TRACKER_PATHS` and
    `note_merge.NOTE_TRACKERS` do.

    Read off the AST rather than the text so a docstring mentioning the name
    does not count, matching `_code_strings`.
    """
    return any(
        isinstance(node, ast.Name) and node.id == "__file__" for node in ast.walk(tree)
    )


def _glob_constrains(pattern: str) -> bool:
    """Does `pattern` keep any literal character to match documents BY?

    A glob made only of wildcards and separators — `"*"`, `"**"`, `"*/*"` —
    matches every path in the repository. It says that its file sweeps SOME
    directory and nothing whatever about which one, so it is not evidence that
    the file reads a document; `_names_document` would otherwise let it name
    every document there is.

    MEASURED 2026-08-27, and this is not hypothetical. `dashboard.py:4026` calls
    `audit_dir.glob("*")` to list AUDIT RUN directories. That bare `"*"` matched
    `docs/SUMMARY.md`, `docs/TESTS.md` and `CLAUDE.md`, which made the module a
    declared reader of every tracker; because it is imported across the suite,
    the reverse-import closure over it then selected 72 of 93 test files on a
    prose change that no test in 52 of them can observe. Requiring one literal
    takes the same change to 20, and a runtime audit hook over all 52 recorded
    zero opens of a real tracker.

    The directory sweeps this branch exists for are UNAFFECTED: `"*.md"` keeps
    `.md` and `"docs/AUDIT_*.md"` keeps both `docs/` and `.md`, so each still
    names what it can reach. Only a pattern that discriminates nothing is
    dropped.
    """
    return any(char not in "*?[]!-/" for char in pattern)


def _names_document(strings: set[str], rel: str, name: str) -> bool:
    """Does one of `strings` name the document at `rel` (basename `name`)?

    EQUALITY, not containment, and that is the load-bearing half of this rule.
    A file that opens a document names it exactly — `"docs/TESTS.md"`,
    `REPO_ROOT / "docs" / "SUMMARY.md"`. A file that merely cites one embeds
    the name in a sentence, and a sentence can live in a string as easily as in
    a docstring: `cli.py` and `config.py` both raise errors reading "see
    docs/SECURITY.md S31", and containment would make a `docs/SECURITY.md`
    round seed two of the most widely imported modules in the package — the
    whole suite, through the closure, on one of the four trackers every task
    writes.

    A glob is matched rather than compared, because a directory sweep names no
    document: `dashboard.py`'s `"docs/AUDIT_*.md"` and an `rglob("*.md")` both
    read a file they never spell out, and dropping the extension token (which
    is what stops `.md` attributing the checkout) would otherwise lose them.
    `fnmatchcase` rather than `fnmatch`: the latter normalizes case per
    platform, which would make a selection depend on which machine ran it.
    """
    if rel in strings or name in strings:
        return True
    return any(
        any(wild in text for wild in "*?[")
        and _glob_constrains(text)
        and (fnmatch.fnmatchcase(rel, text) or fnmatch.fnmatchcase(name, text))
        for text in strings
    )


def _files_reading_documents(
    root: Path, files: Sequence[str], documents: Sequence[str]
) -> dict[str, frozenset[str]]:
    """`{changed prose document: every graph file that READS it}`.

    The narrow carve-out select-02 exists for, and the reason it is a carve-out
    rather than a tightening of `_reference_tokens`: a `.md` path's tokens
    include the bare strings `docs` and `.md`, which — measured 2026-08-27 —
    made 198 of 512 graph files a seed for `docs/TESTS.md`, 99 of them test
    files, because every module that cites a tracker in a docstring was counted
    as depending on it. A change to prose changes only BYTES, and bytes reach a
    test only through a file that OPENS them, so two conditions replace the
    token scan for these paths and nothing else:

    * the file NAMES the document in code (`_code_strings`, `_names_document`)
      — not in a comment, not in a docstring, not inside a sentence; and
    * the file can address the checkout it lives in (`_addresses_own_checkout`).

    Everything downstream is unchanged: the result is closed over the same
    reverse-import edges as any other seed, so a module that reads a document
    still drags in the tests that import it, and a document nothing reads is
    attributed nothing and widens the whole run exactly as before.

    **A file this cannot read or cannot parse is a seed for EVERY document, not
    for none** — the same fail-open `_files_referencing` refuses, closed the
    same way and for the same reason. Leaning on `build_import_graph` having
    marked the same file `opaque` would make correctness rest on a race between
    two walks of the same tree. `ValueError` is in the tuple because
    `UnicodeDecodeError` (a binary file named `.py`) is one, and `SyntaxError`
    because this parses where `_files_referencing` only reads.

    KNOWN GAP, stated because the empty-attribution backstop does NOT catch it:
    a reader that builds the document's name dynamically (`f"{stem}.md"`,
    `"/".join(parts)`) names it in no constant and is missed, and the set is
    then incomplete rather than empty, so nothing widens. It is bounded by the
    condition being conjunctive with `__file__` — the shape has to occur in a
    file that resolves its own checkout — and by
    `test_every_test_that_reads_a_shipped_document_is_selected` reading the
    population off the checkout rather than off a list.
    """
    hits: dict[str, set[str]] = {rel: set() for rel in documents}
    names = {rel: PurePosixPath(rel).name for rel in documents}
    for rel in files:
        try:
            source = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            for found in hits.values():
                found.add(rel)
            continue
        # `__file__` is an IDENTIFIER, so `_addresses_own_checkout` can only
        # find an `ast.Name` for it if the token appears verbatim in the source
        # — a name cannot be spelled any other way. Testing the text first is
        # therefore a necessary condition, not a heuristic, and it skips a full
        # tree walk for the majority of files, which do not mention it at all.
        # The walk still decides: the token also appears in strings and
        # comments, and only the AST can tell those from a real reference.
        if "__file__" not in source or not _addresses_own_checkout(tree):
            continue
        strings = _code_strings(tree)
        for document, found in hits.items():
            if _names_document(strings, document, names[document]):
                found.add(rel)
    return {rel: frozenset(found) for rel, found in hits.items()}


def _listed(paths: Sequence[str], limit: int = _EVIDENCE_MAX_CONSIDERED) -> str:
    """`paths` as evidence text, bounded — it reaches `state.last_validation`."""
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += f" (+{len(paths) - limit} more)"
    return shown


@dataclass(frozen=True)
class TestSelection:
    """What a per-commit validation run decided to execute, and why.

    `evidence()` is the part a human reads: it becomes part of the validation
    summary, which becomes `state.last_validation` and reaches the reviewer in
    the CONTEXT block of the review message. A narrower run that cannot say what
    it narrowed to is exactly the evidence gap that gets a packet refused, so
    this record is built even when nothing was narrowed.
    """

    #: Commands to actually run, in configured order.
    commands: tuple[tuple[str, ...], ...]
    #: `True` when the whole configured suite runs unmodified.
    widened: bool
    #: Why it widened, or "" when it did not.
    reason: str
    #: Changed paths the decision was made from.
    considered: tuple[str, ...] = ()
    #: Test files selected, repo-relative, sorted.
    selected: tuple[str, ...] = ()
    #: How many test files the graph knows about at all.
    total_test_files: int = 0
    #: Individual tests deselected WITHIN the selected files, under
    #: `test_selection = "per_test"`. 0 in every other mode.
    deselected: int = 0
    #: Configured commands dropped because no selected test lives under their
    #: declared paths, each with the reason — reported explicitly, never
    #: silently absent from the summary.
    skipped: tuple[tuple[tuple[str, ...], str], ...] = ()
    #: `False` when the configured list has no pytest command at all, i.e.
    #: there was no test selection to make. `evidence()` is then empty.
    applicable: bool = True
    #: Changed paths the import graph resolved to a module it knows.
    resolved: tuple[str, ...] = ()
    #: `(changed path, number of test files)` for each UNRESOLVED path that was
    #: given a conservative set of its own instead of widening the whole run.
    #: Sorted by path, like everything else a reviewer compares between rounds.
    attributed: tuple[tuple[str, int], ...] = ()
    #: Unresolved paths nothing could be attributed to. Non-empty only on a
    #: widened result, where `reason` names them too — this is the field that
    #: says WHICH path forced a full run, rather than how many did.
    unattributed: tuple[str, ...] = ()
    #: `True` once the import graph has actually been consulted for this
    #: decision. `False` on the widenings that happen BEFORE it is built (no
    #: pytest command, `mode="full"`, a caller-supplied reason, no changed
    #: paths, a graph that could not be read), where reporting "0 resolved"
    #: would read as a failure to resolve rather than as work never done.
    graph_consulted: bool = False

    def _path_accounting(self) -> str:
        """How each changed path was handled, in counts a reader can check.

        Present in BOTH `evidence()` branches: a full-suite run needs to say how
        much of the diff DID resolve just as much as a narrowed one does, since
        that is the difference between "nothing could be established" and "one
        path out of six forced this".
        """
        if not self.graph_consulted:
            return ""
        text = (
            f" Path accounting: {len(self.considered)} changed path(s) — "
            f"{len(self.resolved)} resolved as Python modules the import graph "
            f"knows, {len(self.attributed)} not resolvable and attributed a "
            "conservative set by repository content reference, "
            f"{len(self.unattributed)} not resolvable and attributable to "
            "nothing."
        )
        if self.attributed:
            shown = [
                f"{path} -> {count} test file(s)"
                for path, count in self.attributed[:_EVIDENCE_MAX_CONSIDERED]
            ]
            if len(self.attributed) > _EVIDENCE_MAX_CONSIDERED:
                extra = len(self.attributed) - _EVIDENCE_MAX_CONSIDERED
                shown.append(f"(+{extra} more)")
            text += " Attributed: " + "; ".join(shown) + "."
        return text

    def _deselection_accounting(self) -> str:
        """What `per_test` dropped WITHIN the selected files, or "" for the
        modes that drop nothing. Stated separately from the file count because
        they are different claims: the files are chosen by reachability, the
        tests within them by what each one can reach."""
        if not self.deselected:
            return ""
        return (
            f" Within those files, {self.deselected} individual test(s) were "
            "DESELECTED as unable to reach the change "
            '([audit] test_selection = "per_test"): each one names no module '
            "the change reaches, is not opaque, and sits in a file the commit "
            "did not itself touch. A test whose dependencies cannot be read "
            "statically, or which sits in a file selected by attribution rather "
            "than by import reachability, is never dropped."
        )

    def evidence(self) -> str:
        if not self.applicable:
            return ""
        if self.widened:
            return (
                "test selection: FULL SUITE — every configured test command ran "
                f"unmodified ({self.reason}).{self._path_accounting()}"
            )
        selected = list(self.selected[:_EVIDENCE_MAX_SELECTED])
        if len(self.selected) > _EVIDENCE_MAX_SELECTED:
            selected.append(f"(+{len(self.selected) - _EVIDENCE_MAX_SELECTED} more)")
        considered = list(self.considered[:_EVIDENCE_MAX_CONSIDERED])
        if len(self.considered) > _EVIDENCE_MAX_CONSIDERED:
            considered.append(f"(+{len(self.considered) - _EVIDENCE_MAX_CONSIDERED} more)")
        dropped = ""
        if self.skipped:
            dropped = " Commands SKIPPED, no selected test under their paths: " + "; ".join(
                f"`{' '.join(argv)}` ({why})" for argv, why in self.skipped
            ) + "."
        return (
            "test selection: SUBSET by import-graph reachability — "
            f"{len(self.selected)} of {self.total_test_files} test file(s) are "
            f"selected from the {len(self.considered)} changed path(s) this run "
            f"was given [{', '.join(considered)}] by following the reverse of "
            "every import edge in the repository, transitively, so a test file "
            "the commit did not touch is still selected whenever it can reach "
            f"the change: [{', '.join(selected)}]. Each configured pytest command "
            "that ran, ran only the selected files under its own declared paths "
            "(a command this selector could not retarget widens the whole run "
            "instead, so none is present here), and every non-test command "
            "(ruff) ran unmodified. Sufficient because a test "
            "can only exercise changed code by importing it directly or through "
            "another repository module, and the cases where that cannot be read "
            "statically are not assumed away: a file using a dynamic import or "
            "spawning an interpreter, or one that fails to parse, is selected "
            "unconditionally, and a changed path outside the resolvable Python "
            "import graph (config, fixtures, docs) is not assumed unrelated "
            "either — it is attributed its OWN conservative set, every "
            "repository file whose source names that path, its basename, its "
            "stem, its extension or any directory above it, closed over the "
            "same import edges — except a PROSE DOCUMENT (`.md`), whose tokens "
            "(`docs`, `.md`) name the whole checkout: it is attributed the "
            "files that READ it, naming it exactly in evaluated code rather "
            "than in a comment or docstring and able to address this checkout, "
            "closed over the same edges. A path that can be attributed nothing "
            "at all still widens the whole run."
            f"{self._deselection_accounting()}"
            f"{self._path_accounting()}"
            f"{dropped} {PRECOMMIT_EVIDENCE} To widen, "
            "either OPERATOR lever (a `plan` directive can set neither): "
            '`[audit] test_selection = "full"` in the loop config, which takes '
            "effect on the next round and — since BOTH phases read that one "
            "setting — restores exactly the pre-2026-08-20 behaviour; or this "
            "task's own `validation` commands (`autoloop task-add "
            "--validation ...`, or the same key through the operator inbox), "
            "which are run exactly as declared and never narrowed at either "
            "phase."
        )


def _root_is_narrowable(root: str) -> bool:
    """Is this positional pytest argument a plain repo-relative path?

    A node id (`file.py::test_x`), a glob, or an absolute path is refused
    rather than reinterpreted — the command then runs exactly as configured.
    """
    if not root or root.startswith("-"):
        return False
    if "::" in root or "*" in root or "?" in root:
        return False
    if root.startswith("/") or root.startswith("~"):
        return False
    return True


def _under_root(rel: str, root: str) -> bool:
    normalized = root.rstrip("/")
    if normalized in ("", "."):
        return True
    return rel == normalized or rel.startswith(normalized + "/")


def _positional_indices(args: Sequence[str]) -> list[int] | None:
    """Indices of the test-path arguments in `args` (everything after the
    `pytest` token), or `None` when the argv contains a flag this does not
    recognise.

    Fail-closed on purpose: mistaking a flag's value for a path would hand
    pytest a nonexistent file, and mistaking a path for a flag would silently
    drop a whole tree from the run. An unrecognised flag means the caller keeps
    the command exactly as configured.
    """
    indices: list[int] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            indices.extend(range(index + 1, len(args)))
            return indices
        if token.startswith("-"):
            if token.startswith("--") and "=" in token:
                index += 1
                continue
            if token in _PYTEST_VALUE_FLAGS:
                index += 2
                continue
            if token in _PYTEST_BOOL_FLAGS:
                index += 1
                continue
            # Attached short-flag value: `-nauto`, `-kfoo`.
            if len(token) > 2 and not token.startswith("--") and token[:2] in _PYTEST_VALUE_FLAGS:
                index += 1
                continue
            return None
        indices.append(index)
        index += 1
    return indices


#: The four things that can happen to one configured command, of which exactly
#: ONE widens the whole run.
#:
#: * `UNCHANGED` — not a pytest command at all (`ruff`). Runs as configured, and
#:   that is the whole point: selection decides which TESTS run, not which
#:   checks.
#: * `NARROWED` — rewritten to name the selected files under its own paths.
#: * `SKIP` — a pytest command none of the selected files live under. Dropped
#:   from the run and named in the evidence; the OTHER commands still narrow,
#:   because "no selected test is under `other/`" is a reachability answer, not
#:   an unknown.
#: * `BLOCKED` — a pytest command this selector cannot retarget without
#:   guessing (an unrecognised flag, no declared paths, a node id/glob/absolute
#:   target). This is an UNKNOWN, and the rule for an unknown is the same here
#:   as everywhere else in this section: the whole selection widens back to the
#:   configured commands. Keeping the command as configured while still
#:   reporting a subset — what this did until 2026-08-20 — makes `evidence()`
#:   claim a narrowing that command did not perform.
_RETARGET_UNCHANGED = "unchanged"
_RETARGET_NARROWED = "narrowed"
_RETARGET_SKIP = "skip"
_RETARGET_BLOCKED = "blocked"


def _tests_that_reach_nothing(
    repo_root: Path,
    graph: ImportGraph,
    selected: Sequence[str],
    reachable: frozenset[str],
    never_narrow: frozenset[str],
) -> tuple[str, ...]:
    """`path::name` for every selected test that reaches nothing that changed.

    A test is named here — i.e. dropped — only when the analysis POSITIVELY says
    so: it is not opaque, and no module it can reach is one the change reaches.
    Every other outcome runs. That asymmetry is the whole safety argument, and it
    is why the plugin takes a drop-list rather than a keep-list.

    `never_narrow` holds the files nothing may be dropped from:

    * a test file the commit itself CHANGED — its own edit is the thing under
      test, and no module-level reasoning describes that;
    * a file selected by ATTRIBUTION rather than by import reachability (a
      changed `.md` or `.toml`, matched by `_files_reading_documents` or
      `_reference_tokens`). Attribution is a claim about the FILE naming a
      document; it says nothing about which of its tests do, and the seeds are
      not modules, so `reachable` cannot answer for them.

    A file that cannot be read or parsed contributes nothing, so all of it runs.
    """
    entries: list[str] = []
    for rel in selected:
        if rel in never_narrow:
            continue
        try:
            tests = dependencies_by_test(repo_root, rel, graph.files)
        except (OSError, SyntaxError, ValueError):
            continue
        for name, deps in sorted(tests.items()):
            if deps.opaque or (deps.modules & reachable):
                continue
            entries.append(f"{rel}::{name}")
    return tuple(entries)


def _deselect_args(entries: Sequence[str]) -> tuple[str, ...]:
    """Write the drop-list and return the pytest flags that read it.

    OUTSIDE the checkout, deliberately: validation runs inside the task's worker
    repository and the gate right after it refuses a tree validation dirtied, so
    a list written next to the tests would fail the very round it was narrowing.

    Named by the hash of its own contents, so the same selection reuses one file
    and the command stays a pure function of the round. Returns `()` when the
    file cannot be written, and the caller then runs everything — a list that
    does not exist must never be read as "drop nothing you cannot see".
    """
    body = "\n".join(entries) + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"autoloop-deselect-{digest}.txt"
    try:
        if not path.exists():
            path.write_text(body, encoding="utf-8")
    except OSError:
        return ()
    return ("-p", "autoloop.pytest_deselect", "--autoloop-deselect", str(path))


def _retarget_pytest(
    argv: Sequence[str], selected: Sequence[str]
) -> tuple[str, tuple[str, ...] | None, str]:
    """`(status, command, note)` for one configured command.

    `status` is one of the four `_RETARGET_*` constants above and is what the
    caller branches on — the command alone cannot say which case it is in, since
    `UNCHANGED` and `BLOCKED` both hand back `argv` untouched and mean opposite
    things. `note` says what happened, for the evidence line; it is "" only for
    `UNCHANGED`. `command` is `None` for `SKIP`.
    """
    argv = tuple(argv)
    start = _pytest_index(argv)
    if start is None:
        return _RETARGET_UNCHANGED, argv, ""
    args = argv[start + 1 :]
    indices = _positional_indices(args)
    if indices is None:
        return _RETARGET_BLOCKED, argv, "it has a flag this selector does not recognise"
    if not indices:
        # No paths at all: the surface is whatever `testpaths` says. Injecting
        # paths would CHANGE what the command means, and running it as
        # configured while reporting a subset would misdescribe it.
        return (
            _RETARGET_BLOCKED,
            argv,
            "it declares no test paths, so its surface comes from pytest.ini's "
            "testpaths and injecting paths would change what it means",
        )
    roots = [args[i] for i in indices]
    if not all(_root_is_narrowable(root) for root in roots):
        return _RETARGET_BLOCKED, argv, "it targets a node id, glob or absolute path"
    keep = sorted(rel for rel in selected if any(_under_root(rel, root) for root in roots))
    if not keep:
        return _RETARGET_SKIP, None, f"no selected test file under {', '.join(roots)}"
    positional = set(indices)
    rest = [token for i, token in enumerate(args) if i not in positional]
    # Paths go back where the FIRST positional token was. Everything before it
    # is by construction non-positional, so a command ending in `--` keeps its
    # meaning: the `--` sits in `head` and the paths land after it.
    head = list(args[: indices[0]])
    tail = rest[len(head) :]
    return (
        _RETARGET_NARROWED,
        argv[: start + 1] + tuple(head) + tuple(keep) + tuple(tail),
        "narrowed",
    )


def select_validation_commands(
    commands: Sequence[Sequence[str]],
    changed_paths: Sequence[str],
    repo_root: Path,
    mode: str = TEST_SELECTION_REACHABLE,
    full_reason: str = "",
) -> TestSelection:
    """Decide which of `commands`' tests this commit actually needs.

    `changed_paths` is what GIT says changed — the commit range after the commit
    exists, `git status` before it — never what an executor or an agent reported.
    `full_reason`, when non-empty, forces the full suite and
    is used verbatim as the recorded reason; that is how a caller says "this
    task declared its own validation" without this function having to know what
    a task is.

    Deterministic for a given (commands, changed_paths, tree): the file walk is
    sorted at every level, reachability is a set closure, and the selected list
    is sorted before it is used. Two runs over the same diff produce the same
    commands and the same evidence string.

    Every uncertainty widens, but each widening is as LOCAL as it can honestly
    be. In order: no pytest command to narrow, mode `full`, a caller-supplied
    reason, no changed paths, a graph that could not be built or was truncated,
    a changed `.py` path the graph does not contain, an unresolved path nothing
    in the checkout names, a selection of zero test files — which is treated as
    a gap in the model rather than as proof that no test exercises the change —
    and finally a configured pytest command that cannot be retargeted safely
    (`_RETARGET_BLOCKED`), which widens the WHOLE run rather than leaving that
    one command as configured, because a `TestSelection` reporting a subset must
    describe every command it returned.

    Two things do NOT widen, and both are ANSWERS rather than unknowns:

    * **A changed path the import graph cannot resolve** — a `.md`, a `.toml`, a
      fixture — no longer discards the answer for the paths that did resolve.
      It is given a conservative set of its own (`_reference_tokens` /
      `_files_referencing`: every repository file whose source names it, closed
      over the same import edges) and the result is UNIONED with reachability
      from the resolved paths. A changed PROSE DOCUMENT is the single exception
      to the token half of that rule and is attributed by
      `_files_reading_documents` instead — the files that read it rather than
      the files that mention it — for the reason measured in the "a prose docs
      change must not select the whole suite" block above. The closure, the
      union and the widening are identical either way. Only a path that can be
      attributed nothing goes back to the full suite, and `reason` then names
      that path. See the "one unresolvable path must not veto the whole
      selection" block above for the measurement that forced this and the
      argument that it is safe.
    * **A pytest command none of the selected files live under**: the command is
      dropped, named in `skipped`, and disclosed by `evidence()`.
    """
    commands = tuple(tuple(argv) for argv in commands)
    applicable = any(_pytest_index(argv) is not None for argv in commands)
    # Blanks dropped rather than carried: git never reports one, but a blank
    # would reach `_module_parts`, whose `parts[-1]` raises on the empty path.
    # It would widen first (an empty string is not in `graph.files`), so this is
    # belt-and-braces against a later reordering of the checks below.
    considered = tuple(sorted({path for path in changed_paths if path.strip()}))

    def full(
        reason: str,
        total: int = 0,
        *,
        resolved: tuple[str, ...] = (),
        attributed: tuple[tuple[str, int], ...] = (),
        unattributed: tuple[str, ...] = (),
        consulted: bool = False,
    ) -> TestSelection:
        return TestSelection(
            commands=commands,
            widened=True,
            reason=reason,
            considered=considered,
            total_test_files=total,
            applicable=applicable,
            resolved=resolved,
            attributed=attributed,
            unattributed=unattributed,
            graph_consulted=consulted,
        )

    if not applicable:
        return full("no pytest command is configured, so there is nothing to select")
    if full_reason:
        return full(full_reason)
    if mode not in _NARROWING_MODES:
        return full('[audit] test_selection = "full"')
    if not considered:
        return full("no changed paths were established for this run")
    try:
        graph = build_import_graph(repo_root)
    except OSError as exc:
        return full(f"the repository import graph could not be read ({exc})")
    if graph.truncated:
        return full(
            f"the checkout has more than {_GRAPH_MAX_FILES} Python files, above "
            "the bound this selector will parse"
        )
    total = len(graph.test_files)
    if not total:
        return full("the import graph found no test files at all", total)
    resolved = tuple(path for path in considered if path in graph.files)
    unresolved = tuple(path for path in considered if path not in graph.files)
    # A changed `.py` path the graph does NOT contain is the one unresolved kind
    # that cannot be attributed: it was deleted by this commit, or it lives
    # outside the walk. The files that import it name it as a dotted module
    # (`from autoloop.gone import x`), never as a path, so a content-reference
    # scan cannot find them either — and those importers are exactly what a
    # deletion breaks. Widen, and name the path.
    unusable = tuple(path for path in unresolved if path.endswith(".py"))
    if unusable:
        return full(
            f"{len(unusable)} changed Python path(s) are absent from the import "
            "graph (deleted by this commit, or outside the walk), so neither "
            "reachability nor a content-reference attribution can be established "
            f"for them: {_listed(unusable)}",
            total,
            resolved=resolved,
            unattributed=unusable,
            consulted=True,
        )
    # Everything else unresolved gets a set of its OWN rather than vetoing the
    # paths that resolved. One scan of the checkout serves the whole diff, and
    # an all-Python diff pays for no scan at all.
    #
    # TWO scans, because a prose document is attributed by a different rule
    # (`_files_reading_documents`) from every other unresolvable path
    # (`_reference_tokens` / `_files_referencing`, untouched). Each scan runs
    # only when the diff holds a path of its kind, so a diff of one kind still
    # pays for exactly one pass over the checkout.
    graph_files = sorted(graph.files)
    documents = tuple(path for path in unresolved if _is_prose_document(path))
    referenced = tuple(path for path in unresolved if not _is_prose_document(path))
    references: dict[str, frozenset[str]] = {}
    if referenced:
        references.update(
            _files_referencing(
                repo_root,
                graph_files,
                {path: _reference_tokens(path) for path in referenced},
            )
        )
    if documents:
        references.update(_files_reading_documents(repo_root, graph_files, documents))
    attributed: list[tuple[str, int]] = []
    attributed_tests: set[str] = set()
    unattributed: list[str] = []
    for path in unresolved:
        seeds = references[path]
        # Tested on the SEEDS, not on the closure: `reachable_from` unions
        # `graph.opaque` into every call, so an empty seed set still comes back
        # with the always-selected files and would look like an established
        # answer built from no evidence at all. That is the fail-open this
        # branch exists to refuse.
        tests = (
            frozenset(p for p in graph.reachable_from(seeds) if _is_test_file(p))
            if seeds
            else frozenset()
        )
        if not tests:
            unattributed.append(path)
            continue
        attributed.append((path, len(tests)))
        attributed_tests |= tests
    if unattributed:
        return full(
            f"{len(unattributed)} changed path(s) are not Python modules the "
            "import graph resolves AND no repository file names them (reads "
            "them, for a prose document), so no conservative test set can be "
            f"attributed to them: {_listed(tuple(unattributed))}",
            total,
            resolved=resolved,
            attributed=tuple(attributed),
            unattributed=tuple(unattributed),
            consulted=True,
        )
    reachable = graph.reachable_from(resolved)
    selected = tuple(
        sorted({path for path in reachable if _is_test_file(path)} | attributed_tests)
    )
    if not selected:
        return full(
            "reachability selected no test file at all, which is likelier a gap "
            "in the model than a change no test exercises",
            total,
            resolved=resolved,
            attributed=tuple(attributed),
            consulted=True,
        )
    deselect_args: tuple[str, ...] = ()
    deselected = 0
    if mode == TEST_SELECTION_PER_TEST:
        entries = _tests_that_reach_nothing(
            repo_root, graph, selected, reachable, attributed_tests | set(considered)
        )
        deselected = len(entries)
        if entries:
            deselect_args = _deselect_args(entries)
            if not deselect_args:
                deselected = 0  # the list could not be written; run everything

    kept: list[tuple[str, ...]] = []
    skipped: list[tuple[tuple[str, ...], str]] = []
    blocked: list[str] = []
    for argv in commands:
        status, rewritten, note = _retarget_pytest(argv, selected)
        if status == _RETARGET_NARROWED and deselect_args:
            start = _pytest_index(rewritten)
            rewritten = rewritten[: start + 1] + deselect_args + rewritten[start + 1 :]
        if status == _RETARGET_BLOCKED:
            blocked.append(f"`{' '.join(argv)}` ({note})")
            continue
        if status == _RETARGET_SKIP:
            skipped.append((argv, note))
            continue
        kept.append(rewritten)
    # Checked BEFORE `kept`, and after the loop rather than inside it, so the
    # reason can name every command that could not be narrowed rather than the
    # first one — a reviewer reading "FULL SUITE" is owed the whole cause.
    if blocked:
        return full(
            f"{len(blocked)} configured pytest command(s) cannot be retargeted "
            "without guessing which of their tokens are test paths, so the whole "
            "run widens rather than record a subset they did not execute: "
            + "; ".join(blocked),
            total,
            resolved=resolved,
            attributed=tuple(attributed),
            consulted=True,
        )
    if not kept:
        return full(
            "every configured command would have been skipped, leaving nothing "
            "to validate",
            total,
            resolved=resolved,
            attributed=tuple(attributed),
            consulted=True,
        )
    return TestSelection(
        commands=tuple(kept),
        widened=False,
        reason="",
        deselected=deselected,
        considered=considered,
        selected=selected,
        total_test_files=total,
        skipped=tuple(skipped),
        applicable=True,
        resolved=resolved,
        attributed=tuple(attributed),
        graph_consulted=True,
    )
