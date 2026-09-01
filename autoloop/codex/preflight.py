"""Can this machine actually run the configured codex reviewer, right now?

`doctor` used to answer half of that question — is the binary on PATH, and is
the working directory outside the checkout — and the half it left out is the
half that was broken. Measured 2026-08-17:

    $ cd ~ && codex exec "reply with exactly: ready"
    Not inside a trusted directory and --skip-git-repo-check was not specified.

`CodexConfig.working_dir = ""` meant the HOME DIRECTORY, chosen as "anywhere
but the repository", and codex REFUSES to run there. So the shipped default
could not work, every check `doctor` made about it came back `ok`, and the
first thing that said otherwise was a failed review round.

**A working directory is not a confinement, and this module no longer says it
is.** `cwd` chooses where a process starts; it refuses nothing — not an
absolute path, not `..`, not a subprocess. The confinement is
`codex.sandbox_args`, read as a policy by `sandbox.describe_invocation` —
across `codex.command` too, since codex sees one argv — and REFUSED here when
it is not one. What the directory still buys is real but
small: codex trusts it rather than the operator's home, and a reviewer started
outside the checkout is not one relative path from the tree it is grading.

**Three separate things live here, and they are separate on purpose.**

* `resolve_working_dir` — the ONE place that decides what an empty
  `codex.working_dir` means. Both transports' runners and `doctor` call it, so
  the directory `doctor` grades is by construction the directory the reviewer
  gets. Before it, the same rule was written out three times
  (`SubprocessCodexRunner`, `SubprocessAppServer`, `doctor`), and a divergence
  between them would have been a check reporting `ok` about a path nothing
  uses.
* `ensure_working_dir` — creates the DEFAULT directory and refuses to create a
  CONFIGURED one. A default that provisions itself removes a failure mode
  nobody chose; silently creating a mistyped `codex.working_dir` would hide the
  typo instead, so that raises with the path in the message.
* `preflight_codex` — the sandbox policy, then one trivial invocation, from
  that directory, with the configured `sandbox_args`, graded on its EXIT CODE.
  It is what makes "selecting codex_cli is safe" checkable before a round
  rather than during one. An unconfined policy is a `fail` and NO invocation:
  the check does not launch an unsandboxed reviewer to find out whether an
  unsandboxed reviewer launches.

**The flags the preflight runs are the flags a review turn runs**, because both
build their argv the same way from the same setting (`preflight_argv`;
`conversation.SubprocessCodexRunner.run`). That is the whole of what this
repository can verify about a sandbox — that the policy is PRESENT, and that
the configured build ACCEPTS it — since no codex binary runs here or in CI.
Enforcement is codex's, and `read-only` does not confine reads; see
`sandbox.py`.

**The success test is `returncode == 0` and nothing else.** The preflight is
not allowed to look for its own words in the output: `codex exec` echoes the
whole prompt back (see `quota.py`), so "the prompt said `ready` and `ready` is
on stdout" would be the loop reading its own input as evidence — the exact
class of defect `quota.py` and `reply.py` exist to close. Exit 0 already
carries everything the check claims: the binary launched, the working
directory was accepted, the configured flags parsed, and auth resolved.
Corroboration that a MESSAGE came back is taken from `reply.isolate_reply`,
which refuses any text the prompt contains, and a run that produces none is a
`warn` rather than an `ok` — never a substring search for a token we sent.

**A failed preflight is not automatically a broken configuration.** A spent
allowance is an account state, not a misconfiguration, so `quota.classify`
decides: exhaustion and throttling are `warn` and name themselves, and only an
otherwise-unclassified non-zero exit is `fail`. That is what keeps a
four-hour-old rate limit from reading as "codex_cli is unusable" — and the
patterns come from the same `or DEFAULT_...` fallback the provider factory
uses, because an empty configured list classifies nothing at all.

**Everything that goes wrong is a status, never an exception.** A probe that
raises — a timeout, a permission error, a runner that returns something absurd
— is reported `fail` naming the exception. A check that swallows its own
failure reports `ok` about a machine it never reached, which is the fail-open
this module was written to remove.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..errors import BrowserError
from .quota import (
    DEFAULT_QUOTA_PATTERNS,
    DEFAULT_RATE_LIMIT_PATTERNS,
    classify,
    failure_digest,
)
from .reply import isolate_reply
from .sandbox import describe_invocation

#: Where the reviewer runs when `codex.working_dir` is empty, relative to the
#: operator's home. A DEDICATED, EMPTY directory rather than the home directory
#: itself, for two reasons: codex refuses to run in an untrusted directory, and
#: trusting `~` once would trust every file the operator owns. Trusting one
#: empty directory keeps codex's own repository check as a live guard instead of
#: switching it off with `--skip-git-repo-check`. It is a smaller claim than
#: confinement, which is `sandbox.py`'s.
DEFAULT_WORKING_DIR_PARTS: tuple[str, ...] = (".autoloop", "codex-workdir")

#: The preflight's prompt. Fixed, tiny, and deliberately worded so that
#: * no quota or rate-limit marker occurs in it — a marker the prompt accounts
#:   for is SUPPRESSED by `quota.classify`, so a prompt mentioning "usage limit"
#:   would blind the check that is meant to recognise one
#:   (`test_codex_preflight.py` asserts this against both default lists); and
#: * it does not name the answer it expects, so a reply cannot be a fragment of
#:   the prompt coming back. `isolate_reply` refuses any candidate the prompt
#:   contains, and a prompt containing its own expected answer would make every
#:   healthy run look like an echo.
PREFLIGHT_PROMPT = "Preflight from the autoloop task loop. Reply with a short greeting."

#: Deadline for the preflight invocation ALONE. Deliberately not
#: `codex.timeout_seconds` (900s, sized for a real review turn): this runs
#: inside `doctor`, and `cli._precondition_transport_live` runs `doctor` while
#: an operator is waiting on a blocker answer. A trivial turn that has not
#: finished in two minutes has told us what we needed to know.
PREFLIGHT_TIMEOUT_SECONDS = 120.0

#: What the check found. Strings rather than an enum because they are read back
#: by eye in a `doctor` row and asserted by name in tests.
OK = "ok"
NO_REPLY = "no_reply"
COMMAND_MISSING = "command_missing"
COMMAND_UNRESOLVABLE = "command_unresolvable"
SANDBOX_UNCONFINED = "sandbox_unconfined"
WORKING_DIR_UNUSABLE = "working_dir_unusable"
INVOCATION_FAILED = "invocation_failed"
QUOTA_EXHAUSTED = "quota_exhausted"
RATE_LIMITED = "rate_limited"
PROBE_ERROR = "probe_error"

#: The three statuses a `doctor` row may carry. Exported so `doctor` can refuse
#: anything else rather than passing an unknown status through — `exit_code`
#: only looks for `"fail"`, so a typo'd status would be counted as a pass.
PREFLIGHT_STATUSES = ("ok", "warn", "fail")

#: The two remedies for codex refusing the working directory, named in every
#: refusal because the CLI's own wording for it cannot be pinned from here and
#: an operator reading `exited 1` needs the next action, not a diagnosis.
_DIRECTORY_REMEDIES = (
    "If codex refused the directory itself (it declines to run outside a "
    "trusted one), either trust it once by running `codex` in it by hand, or "
    'add "--skip-git-repo-check" to codex.sandbox_args — the first keeps that '
    "check as a live guard, the second switches it off everywhere."
)


@dataclass(frozen=True)
class PreflightResult:
    """`status` is what `doctor` reports; `kind` is what tests assert on.

    Two fields rather than one because they answer different questions: an
    operator needs "is this a problem", and a test needs "which of the six
    distinguishable outcomes was this", and collapsing them would make
    `warn` mean both "the allowance is spent" and "codex printed nothing".
    """

    status: str
    detail: str
    kind: str

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class PreflightInvocation:
    """What one preflight run produced. Deliberately smaller than
    `conversation.CodexResult`: nothing here needs a duration or an argv, and a
    narrow shape is a cheaper thing for a test double to satisfy."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


#: How a preflight reaches the CLI. Injectable for the reason
#: `conversation.CodexRunner` is: the suite must never launch a real binary,
#: and the binary is not installed everywhere the suite runs.
PreflightRunner = Callable[[tuple[str, ...], Path], PreflightInvocation]


def default_working_dir() -> Path:
    """The dedicated directory an empty `codex.working_dir` means."""
    return Path.home().joinpath(*DEFAULT_WORKING_DIR_PARTS)


def resolve_working_dir(configured) -> Path:
    """What `codex.working_dir` actually resolves to. THE one definition.

    `~` is expanded, so a configured `"~/reviews"` is a real path rather than a
    literal directory called `~`. Nothing is created and nothing is checked
    here: this is a pure function so `doctor` can report the path it grades
    without touching the filesystem, and so the runners can resolve at
    construction time.
    """
    if not configured:
        return default_working_dir()
    return Path(configured).expanduser()


def ensure_working_dir(path) -> Path:
    """The resolved directory, created if it is the DEFAULT one and missing.

    The asymmetry is the point. The default is ours: an empty directory under
    the operator's home that exists only so the reviewer has somewhere to run,
    and provisioning it removes a failure nobody chose. A CONFIGURED path is
    the operator's statement about where reviews happen — creating a mistyped
    one would silently run the reviewer somewhere they did not name, so a
    missing one raises with the path in the message.

    Raises `BrowserError` (the transport-fault family every codex failure
    already arrives as), never `OSError`: a caller handling transport faults
    must not have to know that this one is a filesystem call.
    """
    path = Path(path)
    if path.is_dir():
        return path
    if path.exists():
        raise BrowserError(
            f"the codex working directory {path} exists but is not a directory. "
            "Set codex.working_dir to a directory the reviewer can run in."
        )
    if path != default_working_dir():
        raise BrowserError(
            f"codex.working_dir points at {path}, which does not exist. Create "
            "it (and trust it once with `codex`), or correct the setting — "
            "autoloop creates only its own default directory, so that a typo "
            "here is not turned into a new directory nobody meant."
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrowserError(
            f"the default codex working directory {path} could not be created: "
            f"{exc}. Create it by hand, or set codex.working_dir."
        ) from exc
    return path


def _subprocess_invocation(argv: tuple[str, ...], cwd: Path) -> PreflightInvocation:
    """The real runner: one bounded `codex exec` with a fixed prompt.

    Through argv and never a shell, the same rule the review path follows —
    although the prompt here is a module constant rather than model-authored
    text.
    """
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
        cwd=str(cwd),
    )
    return PreflightInvocation(
        returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or ""
    )


def preflight_argv(codex) -> tuple[str, ...]:
    """The invocation minus the prompt — exactly what a review turn runs.

    `command` then `sandbox_args`, which is the order and the source
    `SubprocessCodexRunner.run` uses, so the policy the preflight validates is
    the policy the reviewer gets rather than a second construction that agrees
    today. Shared with the diagnostics so a `doctor` row can quote the command
    an operator would have to reproduce, and so a wrong `sandbox_args` is caught
    HERE rather than at the first review: the preflight passes the configured
    flags through unchanged, so a flag this codex build does not accept fails
    the check.
    """
    return (*tuple(codex.command or ()), *tuple(codex.sandbox_args or ()))


def preflight_codex(codex, *, run: PreflightRunner | None = None) -> PreflightResult:
    """Could this loop use `codex_cli` right now?

    `codex` is a `config.CodexConfig` (duck-typed, so a test can pass anything
    with the four attributes read here). `run` is the invocation boundary.

    Ordered cheapest-first, and every step that cannot be answered is an answer:

    1. an empty `command`, or a first word that is not on PATH — reported
       without launching anything, and reported as `fail`, never `skip`. "Not
       attempted" is not evidence that the transport works.
    2. a `sandbox_args` that names no enforceable policy — reported without
       launching anything either. A preflight that ran an UNCONFINED reviewer to
       prove the seat is usable would be doing the thing the check exists to
       refuse, and an `ok` would then mean "it works", not "it is safe".
    3. a working directory that cannot be used — reported with the path.
    4. the invocation itself, carrying those flags. Exit 0 is the pass; a
       non-zero exit is classified, so a spent allowance is not filed as a
       broken command.
    """
    command = tuple(getattr(codex, "command", ()) or ())
    if not command:
        return PreflightResult(
            "fail",
            "codex.command is empty — there is no invocation to make. Set it "
            'back to ["codex", "exec"].',
            COMMAND_MISSING,
        )
    binary = command[0]
    found = shutil.which(binary)
    if not found:
        return PreflightResult(
            "fail",
            f"'{binary}' is not on PATH, so no invocation was attempted. "
            "Install the Codex CLI and sign in with `codex login`, or change "
            "codex.command.",
            COMMAND_UNRESOLVABLE,
        )

    # The confinement, before the process rather than after it, and read from
    # the WHOLE invocation — `codex.command` can carry a bypass flag as easily
    # as `codex.sandbox_args` can, and codex sees one argv. `is_enforceable` is
    # false for a policy this loop cannot read as a sandbox — none named, an
    # unknown mode, or a bypass — and the row carries `describe_invocation`'s
    # own message, which names the setting, the shipped value and `codex exec
    # --help`. Nothing is launched: an unsandboxed reviewer is exactly what must
    # not run here.
    policy = describe_invocation(command, getattr(codex, "sandbox_args", ()) or ())
    if not policy.is_enforceable:
        return PreflightResult("fail", policy.detail, SANDBOX_UNCONFINED)

    try:
        workdir = ensure_working_dir(resolve_working_dir(getattr(codex, "working_dir", "")))
    except (BrowserError, OSError) as exc:
        return PreflightResult("fail", str(exc), WORKING_DIR_UNUSABLE)

    argv = (*preflight_argv(codex), PREFLIGHT_PROMPT)
    shown = " ".join(preflight_argv(codex))
    runner = run or _subprocess_invocation
    try:
        invocation = runner(argv, workdir)
        returncode = int(invocation.returncode)
        stdout = invocation.stdout or ""
        stderr = invocation.stderr or ""
    except Exception as exc:  # noqa: BLE001 - every fault is a verdict, not a traceback
        # Includes the timeout, a permission error, and a probe that returned
        # something this function cannot read. Reported rather than raised: a
        # preflight that raises out of `doctor` would take the whole sweep with
        # it, and one that swallowed the fault would report `ok` about a
        # machine it never reached.
        return PreflightResult(
            "fail",
            f"`{shown}` could not be run in {workdir} — "
            f"{type(exc).__name__}: {exc}",
            PROBE_ERROR,
        )

    if returncode == 0:
        # `isolate_reply` refuses any text the prompt contains, so this cannot
        # read the preflight's own words back as a reply. It is corroboration,
        # not the pass condition — the pass condition is the exit code above.
        isolated = isolate_reply(stdout, PREFLIGHT_PROMPT)
        if isolated.text.strip():
            return PreflightResult(
                "ok",
                f"`{shown}` ran in {workdir} and returned a reply "
                f"({len(isolated.text)} chars isolated from stdout)",
                OK,
            )
        return PreflightResult(
            "warn",
            f"`{shown}` exited 0 in {workdir}, but no message could be isolated "
            "from its stdout — it held only transcript furniture, or only an "
            "echo of the preflight prompt. The transport runs; a review turn "
            "may still be rejected for want of a verdict.",
            NO_REPLY,
        )

    failure = classify(
        returncode,
        stdout,
        stderr,
        PREFLIGHT_PROMPT,
        quota_patterns=tuple(getattr(codex, "quota_patterns", ()) or ())
        or DEFAULT_QUOTA_PATTERNS,
        rate_limit_patterns=tuple(getattr(codex, "rate_limit_patterns", ()) or ())
        or DEFAULT_RATE_LIMIT_PATTERNS,
    )
    # Bounded and echo-stripped by the same helper the transcript record uses,
    # so a `doctor` row can never print an unbounded CLI dump.
    digest = failure_digest(
        returncode,
        stdout,
        stderr,
        PREFLIGHT_PROMPT,
        request_id="doctor-preflight",
        classification=failure,
    )
    said = digest["stderr_tail"] or digest["stdout_tail"] or digest.get("note", "")
    if failure.is_exhaustion:
        return PreflightResult(
            "warn",
            f"the codex allowance is SPENT, not misconfigured: `{shown}` exited "
            f"{returncode} in {workdir} and codex said {failure.matched!r}. "
            "Codex draws on the ChatGPT plan's agentic allowance; the loop "
            "parks or hands over rather than treating this as a review "
            "failure. Waiting for the window to reset is the remedy.",
            QUOTA_EXHAUSTED,
        )
    if failure.is_transient:
        return PreflightResult(
            "warn",
            f"codex is THROTTLING this account: `{shown}` exited {returncode} "
            f"in {workdir} and codex said {failure.matched!r}. Transient — the "
            "remedy is time, and the loop keeps this on its ordinary retryable "
            "budget rather than parking.",
            RATE_LIMITED,
        )
    return PreflightResult(
        "fail",
        f"`{shown}` exited {returncode} in {workdir}: {said} "
        + _DIRECTORY_REMEDIES
        + " If it refused a FLAG, correct codex.sandbox_args — the preflight "
        "passes exactly what a review turn would.",
        INVOCATION_FAILED,
    )


__all__ = [
    "COMMAND_MISSING",
    "COMMAND_UNRESOLVABLE",
    "DEFAULT_WORKING_DIR_PARTS",
    "INVOCATION_FAILED",
    "NO_REPLY",
    "OK",
    "PREFLIGHT_PROMPT",
    "PREFLIGHT_STATUSES",
    "PREFLIGHT_TIMEOUT_SECONDS",
    "PROBE_ERROR",
    "QUOTA_EXHAUSTED",
    "RATE_LIMITED",
    "SANDBOX_UNCONFINED",
    "WORKING_DIR_UNUSABLE",
    "PreflightInvocation",
    "PreflightResult",
    "PreflightRunner",
    "default_working_dir",
    "ensure_working_dir",
    "preflight_argv",
    "preflight_codex",
    "resolve_working_dir",
]
