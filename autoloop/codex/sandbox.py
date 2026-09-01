"""What `codex.sandbox_args` says the reviewer may do — read as a POLICY.

**The claim this module replaced, and why it was wrong.** Until prov-02's second
round the loop said its reviewer was confined because it ran in a dedicated
empty directory outside the checkout. That is not confinement. `cwd` chooses
where a process STARTS; it refuses nothing. An absolute path, `../..`, a
subprocess, an editor, `open("~/.ssh/id_ed25519")` — none of them care what the
working directory is. A reviewer running there with no sandbox flag is
UNCONFINED, and saying otherwise put a control's name on a setting that is not
one.

**What is actually claimable, stated narrowly, because the overclaim is the
defect.** Two things, and only these two:

1. **The policy is PRESENT in every invocation.** `codex.sandbox_args` sits
   between `command` and the prompt in the review turn
   (`conversation.SubprocessCodexRunner.run`) and in the preflight
   (`preflight.preflight_argv`) — the same tuple, from the same setting, in
   both. Pinned against the argv `subprocess.run` really receives, not against
   a preview string, in `test_codex_provider.py`, with a non-default policy as
   well as the shipped one — otherwise "the default reached the process" would
   pass for "the setting was honoured".
2. **The configured build ACCEPTS it.** `doctor`'s preflight runs the flags
   against the real binary, so a spelling this build rejects fails a check
   instead of the first review. No codex binary runs in this repository or in
   CI, so that is the only verification available here — which is why the flag
   list is read rather than trusted, and why every failure message names
   `codex exec --help`.

**What is NOT claimed.** Enforcement is codex's, not ours: nothing in this tree
can prove a sandbox held. And `read-only` is narrower than its name reads. It
restricts WRITES. It is NOT a no-command sandbox — a read-only reviewer may
still RUN commands, executed under codex's sandbox rather than refused — and it
does not confine READS, so it may read the checkout, the operator's home and
anything else that account can read. Whether the mode also closes the network is
codex's own behaviour, is not verified from this repository, and is therefore
claimed nowhere in it. The loop depends on none of that: every turn's prompt
carries its own CONTEXT block, the contract and the diff, so the reviewer is
never asked to read or run anything. See `docs/SECURITY.md` and
`docs/AUTOLOOP.md`.

An earlier round of this task said read-only refused command execution. That was
false, and the correction is deliberately load-bearing rather than cosmetic: an
operator choosing this seat because "the reviewer cannot run anything" would be
choosing it on a guarantee codex does not give.

**Fail-closed, deliberately.** Only a spelling named HERE can produce `ok`.
An unknown mode, a missing mode, a dangling `--sandbox`, a bypass flag, or a
mode this file has never heard of are all `fail`, and a `fail` means the runner
refuses to launch and the preflight makes no invocation. The alternative —
treating an unreadable policy as "probably fine" — is the alarm that switches
itself off when its input is absent.

**The weakest named mode wins**, and it wins over argv order. `codex` is a clap
CLI, so a repeated option is normally last-wins, and by that rule
`--sandbox danger-full-access --sandbox read-only` is read-only. This module
grades it `danger-full-access` anyway. It cannot execute the binary to check
whose rule applies, and being wrong in the permissive direction is a reviewer
running unsandboxed while a row says `ok`; being wrong in this direction is a
`fail` an operator clears by deleting a flag they did not mean to leave there.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The shipped policy, and the value `CodexConfig.sandbox_args` defaults to.
#: `read-only` because the reviewer's job is to read a self-contained packet and
#: answer: it needs no writes, no commands and no repository at all. That is
#: what the seat NEEDS — not what the mode refuses; read-only permits commands
#: and reads, as the module docstring says at length.
DEFAULT_SANDBOX_ARGS: tuple[str, ...] = ("--sandbox", "read-only")

#: The three modes `codex exec --sandbox` documents, weakest confinement last.
READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
FULL_ACCESS = "danger-full-access"
#: No mode named at all — the argv asks for nothing, so whatever this build
#: defaults to is what runs, and this loop cannot read that default from here.
UNSET = "unset"
#: A mode-shaped token this module cannot name. Never treated as "probably one
#: of the good ones": see the fail-closed paragraph above.
UNRECOGNISED = "unrecognised"

#: How permissive each answer is. `UNSET` and `UNRECOGNISED` rank with the
#: worst, because "we do not know" and "there is no sandbox" are the same fact
#: from the loop's side of the boundary.
_RANK: dict[str, int] = {
    READ_ONLY: 0,
    WORKSPACE_WRITE: 1,
    FULL_ACCESS: 2,
    UNRECOGNISED: 3,
    UNSET: 4,
}

#: The option that takes a mode as its value, in both spellings clap accepts.
_MODE_OPTIONS = ("--sandbox", "-s")

#: Flags that select a mode without naming one. `--full-auto` is codex's own
#: shorthand for workspace-write with approvals off; the bypass flags turn the
#: sandbox off outright and are graded as such wherever they appear.
_IMPLICIT_MODES: dict[str, str] = {
    "--full-auto": WORKSPACE_WRITE,
    "--dangerously-bypass-approvals-and-sandbox": FULL_ACCESS,
    "--yolo": FULL_ACCESS,
}

#: The modes a value may spell. Compared lower-cased and stripped, and nothing
#: else is admitted — an alias this file has not heard of is `UNRECOGNISED`.
_MODE_VALUES = (READ_ONLY, WORKSPACE_WRITE, FULL_ACCESS)

#: Said in every `fail`, because the one thing this repository genuinely cannot
#: do is confirm a flag spelling: no codex binary runs here or in CI.
_HELP = (
    "Check `codex exec --help` — your build may spell this differently, and "
    "this loop grades only the spellings it can name. Whatever you set is "
    "passed to `doctor`'s preflight, so a flag your build rejects fails that "
    "check rather than the first review."
)


@dataclass(frozen=True)
class SandboxPolicy:
    """What one `sandbox_args` value amounts to.

    `mode` is what tests assert on; `status` is what a `doctor` row reports;
    `named` is every mode the argv named, so a message can say WHY a policy was
    graded by a flag that is not the last one.
    """

    mode: str
    status: str  # ok | warn | fail
    detail: str
    named: tuple[str, ...] = ()

    @property
    def is_enforceable(self) -> bool:
        """May the reviewer be launched under this policy at all?

        `False` for a policy this loop cannot read as a sandbox — no mode, an
        unknown mode, or a bypass. Both the runner and the preflight refuse on
        it, so an unconfined seat fails BEFORE a round rather than during one.
        """
        return self.status != "fail"


def _mode_of(value) -> str:
    """The mode a `--sandbox` VALUE names, or `UNRECOGNISED`."""
    if not isinstance(value, str):
        return UNRECOGNISED
    candidate = value.strip().lower()
    return candidate if candidate in _MODE_VALUES else UNRECOGNISED


def named_modes(args) -> tuple[str, ...]:
    """Every sandbox mode `args` names, in argv order.

    Four spellings are read, because clap accepts all four and an operator's
    config may carry any of them: `--sandbox read-only`, `--sandbox=read-only`,
    `-s read-only` and `-sread-only`. Two flags name a mode without a value
    (`_IMPLICIT_MODES`).

    Anything else — `--skip-git-repo-check`, `--model`, a flag this loop has
    never seen — is passed over rather than graded. This function answers "what
    confinement was asked for", not "are these flags valid"; the preflight
    answers the second by running them.
    """
    modes: list[str] = []
    expecting = False
    for token in args or ():
        if not isinstance(token, str):
            # A non-string in an argv is a fault of its own, and it could be the
            # VALUE of a `--sandbox` that came before it. Counted as unreadable
            # rather than skipped, so it cannot silently leave the mode `ok`.
            if expecting:
                modes.append(UNRECOGNISED)
                expecting = False
            continue
        if expecting:
            modes.append(_mode_of(token))
            expecting = False
        elif token in _MODE_OPTIONS:
            expecting = True
        elif token.startswith("--sandbox="):
            modes.append(_mode_of(token.split("=", 1)[1]))
        elif token.startswith("-s") and not token.startswith("--") and len(token) > 2:
            modes.append(_mode_of(token[2:]))
        elif token in _IMPLICIT_MODES:
            modes.append(_IMPLICIT_MODES[token])
    if expecting:
        # `--sandbox` with nothing after it. codex would refuse the argv; this
        # says so without launching it, and never reads it as "no mode asked
        # for" — which would be the same `fail`, but for the wrong reason.
        modes.append(UNRECOGNISED)
    return tuple(modes)


def describe_sandbox(args) -> SandboxPolicy:
    """Grade one `codex.sandbox_args` value.

    The WEAKEST named mode decides, for the reason in the module docstring: this
    module cannot execute the binary to learn whose precedence rule applies, and
    the safe direction of being wrong is a `fail` an operator clears by removing
    a flag.
    """
    # The modes are read from the ORIGINAL tokens and only the display is
    # stringified: stringifying first would turn a non-string token into a
    # plausible-looking word and hide it from `named_modes`'s own guard.
    shown = " ".join(str(token) for token in (args or ()))
    modes = named_modes(args)
    mode = max(modes, key=lambda named: _RANK[named]) if modes else UNSET

    if mode == READ_ONLY:
        return SandboxPolicy(
            mode,
            "ok",
            f"`{shown}` — read-only, and named in every invocation (the review "
            "turn and the preflight, from this one setting). What that buys is "
            "what codex's own sandbox enforces: WRITES are restricted. It is "
            "not a no-command sandbox — commands still run under it, sandboxed "
            "rather than refused — and it does NOT confine READS, so the "
            "reviewer may read the checkout and the operator's home. Nothing in "
            "this loop depends on either: every prompt is self-contained, so "
            "the reviewer is never asked to read or run anything.",
            modes,
        )
    if mode == WORKSPACE_WRITE:
        return SandboxPolicy(
            mode,
            "warn",
            f"`{shown}` — workspace-write. A real sandbox, and wider than this "
            "seat needs: the reviewer answers from a self-contained packet and "
            "has nothing to write. Prefer the shipped "
            '["--sandbox", "read-only"] unless you know why this seat writes.',
            modes,
        )
    if mode == FULL_ACCESS:
        return SandboxPolicy(
            mode,
            "fail",
            f"`{shown}` turns the sandbox OFF (danger-full-access or a bypass "
            "flag), so the reviewer runs with this account's full filesystem "
            "and network access. codex_cli is REFUSED under it: the runner will "
            "not launch and the preflight makes no invocation. Set "
            '["--sandbox", "read-only"]. ' + _HELP,
            modes,
        )
    if mode == UNRECOGNISED:
        return SandboxPolicy(
            mode,
            "fail",
            f"`{shown}` names a sandbox mode this loop cannot read, so what "
            "would confine the reviewer is unknown — which is graded exactly "
            "like unconfined, never like probably-fine. codex_cli is REFUSED "
            'until it is one of: read-only, workspace-write. ' + _HELP,
            modes,
        )
    return SandboxPolicy(
        UNSET,
        "fail",
        "codex.sandbox_args names no sandbox mode, so this seat is UNCONFINED: "
        "the reviewer runs with this account's own filesystem and network "
        "access. codex.working_dir does not close that — a working directory "
        "chooses where a process STARTS and refuses nothing, not an absolute "
        "path, not `..`, not a subprocess. codex_cli is REFUSED until a policy "
        'is set; the shipped one is ["--sandbox", "read-only"]. ' + _HELP,
        modes,
    )


def describe_invocation(command, sandbox_args) -> SandboxPolicy:
    """Grade the WHOLE invocation, not the one setting that is meant to carry it.

    `codex.command` and `codex.sandbox_args` are separate keys for readability —
    "how do I run it" and "what may it do" — and codex sees one argv. So a
    `command` of `["codex", "exec", "--yolo"]` with the shipped
    `["--sandbox", "read-only"]` beside it is an argv that names both a sandbox
    and a bypass, and grading only the second key would report `ok` about it.
    Read together, the weakest-wins rule answers `danger-full-access` and every
    caller refuses. That is a fail-open closed at the seam it would open at:
    two settings, one process.
    """
    return describe_sandbox((*tuple(command or ()), *tuple(sandbox_args or ())))


__all__ = [
    "DEFAULT_SANDBOX_ARGS",
    "FULL_ACCESS",
    "READ_ONLY",
    "UNRECOGNISED",
    "UNSET",
    "WORKSPACE_WRITE",
    "SandboxPolicy",
    "describe_invocation",
    "describe_sandbox",
    "named_modes",
]
