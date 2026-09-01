"""The codex preflight: what makes selecting `codex_cli` safe to do.

Two verified defects, measured 2026-08-17, are the subject:

* `working_dir = ""` meant the HOME DIRECTORY, and codex refuses to run there
  ("Not inside a trusted directory and --skip-git-repo-check was not
  specified"). The shipped default could not work, and every check `doctor`
  made about it came back `ok`.
* nothing anywhere ran the configured command from the configured directory, so
  the first thing that could disagree with those checks was a failed review.

And a third, found in review of the round that fixed those two: the loop called
the working directory its confinement. It is not one — `cwd` chooses where a
process STARTS and refuses nothing — so the seat was unconfined while a row
said the emptiness of `codex.sandbox_args` was a deliberate policy. The policy
is now the flags (`codex/sandbox.py`), it ships `--sandbox read-only`, and an
unconfined one is refused before any process starts. What that can and cannot
prove is stated where those tests begin.

No codex binary is involved in this file. The invocation boundary
(`preflight.PreflightRunner`) is faked one level below the logic under test,
which is the only level at which "an exhausted allowance is classified as an
allowance, not as a broken command" can be asserted at all — a real binary
would have to actually be out of quota.

`$HOME` is repointed at `tmp_path` wherever the DEFAULT directory is the
subject, so nothing here creates a directory in the developer's real home. The
environment variable rather than `Path.home`, because `resolve_working_dir`
expands a configured `~` through `os.path.expanduser` and patching the method
would leave that half reading the real home.
"""

import subprocess
from pathlib import Path

import pytest

from autoloop.codex import preflight, sandbox
from autoloop.codex.conversation import SubprocessCodexRunner
from autoloop.codex.preflight import (
    PREFLIGHT_PROMPT,
    PREFLIGHT_STATUSES,
    PreflightInvocation,
    default_working_dir,
    ensure_working_dir,
    preflight_argv,
    preflight_codex,
    resolve_working_dir,
)
from autoloop.codex.sandbox import DEFAULT_SANDBOX_ARGS, describe_sandbox
from autoloop.codex.quota import (
    DEFAULT_QUOTA_PATTERNS,
    DEFAULT_RATE_LIMIT_PATTERNS,
    _squeeze,
)
from autoloop.config import CodexConfig
from autoloop.errors import BrowserError

#: The refusal this task exists because of, quoted from the machine that
#: produced it. Matched by nothing — the preflight classifies on the EXIT CODE
#: and never on this wording, which is why it can be a fixture rather than a
#: pattern list.
TRUSTED_DIR_REFUSAL = (
    "Not inside a trusted directory and --skip-git-repo-check was not specified."
)


class FakeRun:
    """One scripted invocation, and a record of how it was asked for."""

    def __init__(self, returncode=0, stdout="hello there\n", stderr=""):
        self.result = PreflightInvocation(returncode, stdout, stderr)
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), Path(cwd)))
        return self.result


@pytest.fixture
def on_path(monkeypatch):
    """`codex` resolves, without requiring it to be installed."""
    monkeypatch.setattr(preflight.shutil, "which", lambda binary: f"/fake/bin/{binary}")


def config(tmp_path, **overrides):
    overrides.setdefault("working_dir", str(tmp_path))
    return CodexConfig(**overrides)


# ---- the command: resolvable or not ------------------------------------------


def test_an_unresolvable_command_fails_without_launching_anything(tmp_path, monkeypatch):
    """`fail`, never `skip`, and never `ok`: "not attempted" is not evidence
    that the transport works. The check is also deterministic — nothing is
    spawned to discover that there is nothing to spawn."""
    monkeypatch.setattr(preflight.shutil, "which", lambda binary: None)
    run = FakeRun()

    result = preflight_codex(config(tmp_path), run=run)

    assert (result.status, result.kind) == ("fail", preflight.COMMAND_UNRESOLVABLE)
    assert "not on PATH" in result.detail
    assert "codex login" in result.detail
    assert run.calls == []


def test_a_resolvable_command_is_invoked_from_the_configured_directory(tmp_path, on_path):
    run = FakeRun()

    result = preflight_codex(config(tmp_path), run=run)

    assert (result.status, result.kind) == ("ok", preflight.OK)
    (argv, cwd), = run.calls
    assert argv[:2] == ("codex", "exec")
    assert argv[-1] == PREFLIGHT_PROMPT
    assert cwd == tmp_path
    # …and under the shipped policy, because the preflight validates the seat
    # an operator would actually get. A check that ran the reviewer WITHOUT the
    # flags a review turn carries would be grading a different invocation.
    assert argv[2:-1] == DEFAULT_SANDBOX_ARGS


def test_an_empty_command_is_a_failure_rather_than_an_empty_invocation(tmp_path, on_path):
    run = FakeRun()

    result = preflight_codex(config(tmp_path, command=()), run=run)

    assert (result.status, result.kind) == ("fail", preflight.COMMAND_MISSING)
    assert run.calls == []


def test_the_configured_sandbox_flags_are_what_the_preflight_runs(tmp_path, on_path):
    """Why the flags are worth setting at all: whatever an operator puts there
    is validated by a real invocation, so a spelling their build rejects fails
    this check instead of the first review. No codex binary runs in this
    repository or in CI, so that — plus "the flags are present" — is the whole
    of what can be verified from here."""
    run = FakeRun()
    codex = config(tmp_path, sandbox_args=("--sandbox", "workspace-write"))

    preflight_codex(codex, run=run)

    (argv, _cwd), = run.calls
    assert argv == ("codex", "exec", "--sandbox", "workspace-write", PREFLIGHT_PROMPT)
    # And the preview used in diagnostics carries the flags but never the
    # prompt, matching the runner's own `argv_preview` rule.
    assert preflight_argv(codex) == ("codex", "exec", "--sandbox", "workspace-write")


# ---- the sandbox policy: what "safe to select" means -------------------------
#
# The claim these pin is narrow on purpose. Nothing here proves a sandbox is
# ENFORCED — no codex binary runs in this repository — and `read-only` does not
# confine reads in any case. What is proven is that an unconfined policy is
# REFUSED before a process starts, and that the policy an operator configured is
# the policy the invocation carries.


@pytest.mark.parametrize(
    "args, mode, status",
    [
        # Every spelling clap accepts for the same policy.
        (("--sandbox", "read-only"), sandbox.READ_ONLY, "ok"),
        (("--sandbox=read-only",), sandbox.READ_ONLY, "ok"),
        (("-s", "read-only"), sandbox.READ_ONLY, "ok"),
        (("-sread-only",), sandbox.READ_ONLY, "ok"),
        (("--sandbox", "Read-Only"), sandbox.READ_ONLY, "ok"),
        # A flag that names no mode neither provides nor weakens one.
        (("--skip-git-repo-check", "--sandbox", "read-only"), sandbox.READ_ONLY, "ok"),
        # A real sandbox, wider than this seat needs.
        (("--sandbox", "workspace-write"), sandbox.WORKSPACE_WRITE, "warn"),
        (("--full-auto",), sandbox.WORKSPACE_WRITE, "warn"),
        # No mode asked for — the shape that used to be "empty by policy".
        ((), sandbox.UNSET, "fail"),
        (("--skip-git-repo-check",), sandbox.UNSET, "fail"),
        # The sandbox switched off, in each spelling.
        (("--sandbox", "danger-full-access"), sandbox.FULL_ACCESS, "fail"),
        (("--dangerously-bypass-approvals-and-sandbox",), sandbox.FULL_ACCESS, "fail"),
        (("--yolo",), sandbox.FULL_ACCESS, "fail"),
        # Unreadable: a near-miss spelling, a dangling option, a non-string.
        (("--sandbox", "readonly"), sandbox.UNRECOGNISED, "fail"),
        (("--sandbox",), sandbox.UNRECOGNISED, "fail"),
        (("--sandbox", None), sandbox.UNRECOGNISED, "fail"),
        (("-s",), sandbox.UNRECOGNISED, "fail"),
        # The WEAKEST named mode decides, whatever clap's last-wins rule would
        # do with it: this module cannot run the binary to find out, and the
        # safe direction of being wrong is a `fail` an operator clears by
        # deleting a flag.
        (
            ("--sandbox", "danger-full-access", "--sandbox", "read-only"),
            sandbox.FULL_ACCESS,
            "fail",
        ),
    ],
)
def test_the_policy_is_read_fail_closed_from_the_flags(args, mode, status):
    policy = describe_sandbox(args)

    assert (policy.mode, policy.status) == (mode, status)
    assert policy.is_enforceable is (status != "fail")
    assert policy.detail.strip(), "a row an operator cannot act on is not a check"


def test_the_shipped_default_is_the_one_policy_that_passes():
    """`CodexConfig` ships this value, so the default seat is confined without
    an operator action. The assertion is on the CONFIG, not on a constant in
    isolation — a default that agreed with `sandbox.py` and not with the
    dataclass would be exactly the divergence this file exists to catch."""
    assert CodexConfig().sandbox_args == DEFAULT_SANDBOX_ARGS
    assert describe_sandbox(CodexConfig().sandbox_args).status == "ok"


#: Claims about read-only that codex does NOT give, in the shapes two rounds of
#: this task have actually written. `read-only` restricts WRITES; it runs
#: commands inside the sandbox rather than refusing them, and it permits reads.
#: A row saying otherwise is worse than a row saying nothing: it is the reason
#: an operator would select this seat, stated as a guarantee.
RETIRED_OVERCLAIMS = (
    "command execution are refused",
    "commands are refused",
    "refuses command execution",
    "cannot run commands",
    "the reviewer cannot see the checkout",
)


@pytest.mark.parametrize(
    "args",
    [
        DEFAULT_SANDBOX_ARGS,
        ("--sandbox=read-only",),
        ("-s", "read-only"),
        ("--skip-git-repo-check", "--sandbox", "read-only"),
    ],
)
def test_the_read_only_row_states_only_what_read_only_actually_does(args):
    """The correction this round exists for, pinned in the row an operator
    reads rather than only in the prose above it.

    The first round of this task shipped `--sandbox read-only` with a row
    saying it refused WRITES AND COMMAND EXECUTION. The writes half is right and
    the commands half is not: codex's read-only mode runs commands inside the
    sandbox, restricting what they may write. Overclaiming there is the same
    defect class as the working-directory claim this file already covers — a
    control's guarantee asserted wider than the control gives — and it is the
    more dangerous half, because "the reviewer cannot run anything" is a reason
    to select the seat.

    Every spelling is checked, not just the shipped tuple: the sentence lives in
    ONE branch of `describe_sandbox`, and an assertion naming a single spelling
    would still pass if a later edit fixed only the row it names."""
    detail = describe_sandbox(args).detail

    # What is true, and therefore what must be SAID: writes restricted, reads
    # not confined, commands not refused.
    assert "WRITES are restricted" in detail
    assert "does NOT confine READS" in detail
    assert "commands still run" in detail
    # And what must never be said again.
    for overclaim in RETIRED_OVERCLAIMS:
        assert overclaim not in detail, f"read-only does not guarantee: {overclaim}"


def test_no_sandbox_row_anywhere_promises_that_commands_are_refused(tmp_path, on_path):
    """The row is not the only text an operator meets, so the pin is not left on
    the one branch a reviewer would look at. Every grade `describe_sandbox`
    produces, and the preflight refusal that quotes it, must be free of the
    retired claim — including `workspace-write`'s `warn` and the `fail` rows,
    where "commands are refused" would be false in the other direction too."""
    graded = [
        describe_sandbox(args).detail
        for args in (
            DEFAULT_SANDBOX_ARGS,
            ("--sandbox", "workspace-write"),
            ("--full-auto",),
            (),
            ("--yolo",),
            ("--sandbox", "readonly"),
            ("--sandbox",),
        )
    ]
    graded.append(preflight_codex(config(tmp_path, sandbox_args=()), run=FakeRun()).detail)

    # A sweep of ABSENCE assertions passes on text that is not there at all, so
    # the sweep says first that it read something, and that the last item really
    # is the sandbox refusal rather than a command-unresolvable message that
    # never reached the branch under test.
    assert len(graded) == 8
    assert all(detail.strip() for detail in graded)
    assert "UNCONFINED" in graded[-1]

    for detail in graded:
        for overclaim in RETIRED_OVERCLAIMS:
            assert overclaim not in detail


@pytest.mark.parametrize(
    "args",
    [(), ("--skip-git-repo-check",), ("--sandbox", "danger-full-access"), ("--yolo",)],
)
def test_an_unconfined_policy_fails_the_preflight_without_invoking_anything(
    tmp_path, on_path, args
):
    """The fail-open this closes: a preflight that ran an UNSANDBOXED reviewer
    to prove the seat works would answer "it runs" to the question "is it safe",
    and the `ok` would be evidence for the wrong claim."""
    run = FakeRun()

    result = preflight_codex(config(tmp_path, sandbox_args=args), run=run)

    assert (result.status, result.kind) == ("fail", preflight.SANDBOX_UNCONFINED)
    assert run.calls == [], "nothing may be launched under a policy that failed"
    assert "sandbox" in result.detail.lower()


def test_a_bypass_in_the_command_is_graded_too_not_only_the_sandbox_key(
    tmp_path, on_path
):
    """The two keys are one argv. `codex.command` is documented as "how do I run
    it" and `codex.sandbox_args` as "what may it do", but codex reads a single
    command line — so a bypass flag in the first key beside a perfectly good
    policy in the second is an unsandboxed reviewer, and grading only the key
    that is MEANT to carry the policy is how the other one becomes a way round
    it."""
    run = FakeRun()
    codex = config(tmp_path, command=("codex", "exec", "--yolo"))
    assert describe_sandbox(codex.sandbox_args).status == "ok", "the setting alone passes"

    result = preflight_codex(codex, run=run)

    assert (result.status, result.kind) == ("fail", preflight.SANDBOX_UNCONFINED)
    assert run.calls == []


def test_the_refusal_names_the_setting_the_value_and_where_to_check(tmp_path, on_path):
    """An operator reading `fail` needs the next action. The empty case is the
    one a config carried until this round, so its message names what to set."""
    result = preflight_codex(config(tmp_path, sandbox_args=()), run=FakeRun())

    assert "codex.sandbox_args" in result.detail
    assert '["--sandbox", "read-only"]' in result.detail
    assert "codex exec --help" in result.detail


# ---- the working directory ---------------------------------------------------


def test_a_refused_working_directory_fails_and_names_both_remedies(tmp_path, on_path):
    """The measured defect. Classification is by EXIT CODE, not by codex's
    wording — the wording is a property of a CLI version — so the row quotes
    what codex said and names the two things an operator can do about it."""
    run = FakeRun(returncode=1, stdout="", stderr=TRUSTED_DIR_REFUSAL)

    result = preflight_codex(config(tmp_path), run=run)

    assert (result.status, result.kind) == ("fail", preflight.INVOCATION_FAILED)
    assert "Not inside a trusted directory" in result.detail, "codex's own words"
    assert "trust it once" in result.detail
    assert "--skip-git-repo-check" in result.detail
    assert str(tmp_path) in result.detail


def test_a_missing_configured_directory_fails_and_is_not_created(tmp_path, on_path):
    missing = tmp_path / "nowhere"
    run = FakeRun()

    result = preflight_codex(config(tmp_path, working_dir=str(missing)), run=run)

    assert (result.status, result.kind) == ("fail", preflight.WORKING_DIR_UNUSABLE)
    assert str(missing) in result.detail
    assert not missing.exists(), "a typo must not become the place reviews run"
    assert run.calls == []


def test_the_default_directory_is_dedicated_created_and_shared_by_every_caller(
    tmp_path, monkeypatch, on_path
):
    """`resolve_working_dir` is the ONE definition of what an empty
    `codex.working_dir` means. The runner, the app-server transport and
    `doctor` all read it, so a check can never grade a directory the reviewer
    does not get — which is what the home-directory default did.

    It is also not the home directory any more: an empty dedicated directory
    can be trusted once without trusting every file the operator owns."""
    monkeypatch.setenv("HOME", str(tmp_path))
    run = FakeRun()

    expected = tmp_path / ".autoloop" / "codex-workdir"
    assert default_working_dir() == expected
    assert resolve_working_dir("") == expected
    assert resolve_working_dir(None) == expected
    assert SubprocessCodexRunner()._cwd == expected
    assert not expected.exists()

    result = preflight_codex(CodexConfig(), run=run)

    assert result.status == "ok", result.detail
    assert expected.is_dir(), "the default directory provisions itself"
    (_argv, cwd), = run.calls
    assert cwd == expected


def test_a_configured_tilde_is_expanded_rather_than_taken_literally(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_working_dir("~/reviews") == tmp_path / "reviews"


def test_ensure_refuses_a_path_that_is_not_a_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")

    with pytest.raises(BrowserError) as excinfo:
        ensure_working_dir(afile)

    assert str(afile) in str(excinfo.value)


# ---- classification: an account state is not a broken command ----------------


@pytest.mark.parametrize(
    "stderr, kind",
    [
        ("Error: you have hit your usage limit for this week", preflight.QUOTA_EXHAUSTED),
        ("Error: 429 Too Many Requests", preflight.RATE_LIMITED),
    ],
)
def test_an_exhausted_or_throttled_account_warns_and_names_itself(
    tmp_path, on_path, stderr, kind
):
    """The third of this task's three test claims: an exhausted allowance is
    classified BY THE QUOTA PATTERNS and is not reported as a failing check.

    `doctor` exiting 1 on a spent window would tell an operator to repair a
    machine that is correctly configured, and would hold shut every blocker
    whose recheck runs the sweep — on a condition no operator action clears."""
    run = FakeRun(returncode=1, stdout="", stderr=stderr)

    result = preflight_codex(config(tmp_path), run=run)

    assert result.status == "warn", result.detail
    assert result.kind == kind
    assert result.status in PREFLIGHT_STATUSES


def test_an_empty_configured_pattern_list_still_classifies(tmp_path, on_path):
    """The fail-open one `or` prevents: `CodexConfig` ships both lists empty,
    meaning "use the built-in ones" everywhere else in the loop, and passing
    them through raw would classify nothing at all — an exhausted account
    would read as a broken command."""
    codex = config(tmp_path, quota_patterns=(), rate_limit_patterns=())
    run = FakeRun(returncode=1, stderr="Error: quota exceeded for this account")

    assert preflight_codex(codex, run=run).kind == preflight.QUOTA_EXHAUSTED


def test_a_configured_pattern_list_is_honoured(tmp_path, on_path):
    codex = config(tmp_path, quota_patterns=("kontingent verbraucht",))
    run = FakeRun(returncode=1, stderr="Fehler: kontingent verbraucht")

    result = preflight_codex(codex, run=run)

    assert result.kind == preflight.QUOTA_EXHAUSTED
    assert "kontingent verbraucht" in result.detail


def test_an_unclassified_failure_is_a_failing_check(tmp_path, on_path):
    run = FakeRun(returncode=2, stdout="", stderr="Error: connection reset by peer")

    result = preflight_codex(config(tmp_path), run=run)

    assert (result.status, result.kind) == ("fail", preflight.INVOCATION_FAILED)
    assert "connection reset" in result.detail


def test_the_preflight_prompt_cannot_blind_the_quota_check(tmp_path):
    """The echo guard, pointed at this module's own prompt.

    `quota.classify` refuses any marker the PROMPT accounts for — that is what
    stops `codex exec`'s echo of a review packet from declaring the account
    spent. The same rule would silently disarm this check if the preflight
    prompt mentioned a limit, so the prompt is asserted clean against both
    built-in lists rather than merely read and believed."""
    squeezed = _squeeze(PREFLIGHT_PROMPT)

    for pattern in DEFAULT_QUOTA_PATTERNS + DEFAULT_RATE_LIMIT_PATTERNS:
        assert _squeeze(pattern) not in squeezed, pattern


# ---- the reply, and the echo it must not read as one -------------------------


def test_stdout_that_is_only_our_own_prompt_coming_back_is_not_a_reply(tmp_path, on_path):
    """The ECHO trap this check is one step away from: `codex exec` prints the
    prompt back, so "the preflight said X and X is on stdout" would be the loop
    reading its own input as evidence. Exit 0 still passes the transport — the
    exit code is the pass condition — but the row says a message could not be
    isolated instead of claiming one came back."""
    run = FakeRun(returncode=0, stdout=f"user\n{PREFLIGHT_PROMPT}\n")

    result = preflight_codex(config(tmp_path), run=run)

    assert (result.status, result.kind) == ("warn", preflight.NO_REPLY)
    assert "no message could be isolated" in result.detail


def test_empty_stdout_with_a_zero_exit_is_reported_rather_than_called_a_reply(
    tmp_path, on_path
):
    run = FakeRun(returncode=0, stdout="")

    assert preflight_codex(config(tmp_path), run=run).kind == preflight.NO_REPLY


def test_a_real_reply_passes(tmp_path, on_path):
    run = FakeRun(returncode=0, stdout="codex\nGood morning.\ntokens used: 12\n")

    result = preflight_codex(config(tmp_path), run=run)

    assert (result.status, result.kind) == ("ok", preflight.OK)


# ---- the probe itself is not allowed to fail quietly -------------------------


@pytest.mark.parametrize(
    "boom",
    [
        subprocess.TimeoutExpired(cmd="codex", timeout=preflight.PREFLIGHT_TIMEOUT_SECONDS),
        PermissionError(13, "Permission denied"),
        FileNotFoundError(2, "No such file or directory"),
    ],
)
def test_a_probe_that_raises_is_a_failure_naming_the_fault(tmp_path, on_path, boom):
    """A check that swallows its own failure reports `ok` about a machine it
    never reached. Every one of these is a `fail` carrying the exception's
    name, and none of them escapes into `doctor`'s sweep."""

    def explode(argv, cwd):
        raise boom

    result = preflight_codex(config(tmp_path), run=explode)

    assert (result.status, result.kind) == ("fail", preflight.PROBE_ERROR)
    assert type(boom).__name__ in result.detail


def test_a_probe_returning_something_unreadable_is_a_failure_not_a_pass(tmp_path, on_path):
    """`returncode` is read inside the guarded block on purpose: a runner that
    returns `None`, a string or an object without the field must not reach the
    `returncode == 0` comparison, where anything not equal to zero would take
    the classification path and anything equal to it would PASS."""

    def nonsense(argv, cwd):
        return "definitely not an invocation"

    result = preflight_codex(config(tmp_path), run=nonsense)

    assert (result.status, result.kind) == ("fail", preflight.PROBE_ERROR)


# ---- the runner's own half of the same defect --------------------------------


def test_the_runner_reports_a_missing_working_dir_as_itself(tmp_path):
    """`subprocess.run` raises `FileNotFoundError` for a missing cwd exactly as
    it does for a missing binary, and the adapter used to report both as "the
    codex CLI was not found" — sending the investigation after a binary that is
    installed. Checked before the process, so the message names the directory.
    """
    runner = SubprocessCodexRunner(cwd=tmp_path / "gone")

    with pytest.raises(BrowserError) as excinfo:
        runner.run("anything")

    message = str(excinfo.value)
    assert str(tmp_path / "gone") in message
    assert "was not found" not in message, "that is the missing-binary message"


def test_the_runner_creates_its_default_directory_but_never_a_configured_one(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    default = default_working_dir()
    assert not default.exists()

    assert ensure_working_dir(default) == default
    assert default.is_dir()

    with pytest.raises(BrowserError):
        ensure_working_dir(tmp_path / "configured-and-absent")
    assert not (tmp_path / "configured-and-absent").exists()
