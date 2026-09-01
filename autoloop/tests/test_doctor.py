"""Doctor preflight with every boundary mocked: no Chrome, no network, no
playwright, no codex process — and proof that it never submits a message to the
reviewer conversation.

That last qualifier is prov-02's (2026-09-01), and it is a real narrowing: the
sweep now makes ONE trivial codex invocation of its own (`codex_preflight`),
which is not the reviewer conversation and carries no review packet. It is the
second injectable boundary in `DoctorProbes` and is stubbed by `probes()` in
every test here — see that helper for why the default cannot be the real one.

brw-19c (2026-08-31) is the follow-up the note here used to promise. Until it,
`doctor.py` keyed three checks — `conversation_url`,
`conversation_active`/`conversation_rotations` and `project_url` — on the
LITERAL provider name `browser_chatgpt`, unregistered since brw-16
(2026-08-25), plus two UNCONDITIONAL probes (`cdp`, `playwright`) and a
`primary_live` skip that read them. All six are gone. What the tests below now
pin is the shape of their absence, which is the part that can regress
silently:

* naming the retired provider buys no special handling of any kind — no URL
  check, no rotation report, and no `skip` standing in for a probe that was
  never made;
* a healthy repository passes with no browser anywhere on the machine, which
  is the whole point: the old `cdp` probe failed on every such machine and
  took `exit_code` to 1 with it;
* `[browser]` config values are still ACCEPTED and are simply never graded.
"""

import dataclasses
import json
import socket

import pytest

from autoloop import doctor
from autoloop.codex import preflight
from autoloop.codex.preflight import PreflightResult, default_working_dir
from autoloop.config import (
    RETIRED_BROWSER_PROVIDER,
    AutoloopConfig,
    BrowserConfig,
    CodexConfig,
    ConversationConfig,
)
from autoloop.doctor import DoctorProbes, exit_code, run_doctor
from autoloop.errors import LoginExpiredError
from autoloop.lock import LoopLock
from autoloop.policy import PolicyConfig

URL = "https://chatgpt.com/c/valid-id-123"

#: Every check name `doctor.py` produced for a browser and no longer does.
#: Asserted as a SET against the whole result list rather than one name at a
#: time, so a branch restored under any one of them fails here.
REMOVED_CHECKS = frozenset(
    {
        "cdp",
        "playwright",
        "conversation_url",
        "conversation_active",
        "conversation_rotations",
        "project_url",
    }
)


class FakeConversation:
    def __init__(self, login_expired=False):
        self.login_expired = login_expired
        self.opened = 0
        self.submits = []
        self.closed = False

    def attach(self):
        self.opened += 1
        if self.login_expired:
            raise LoginExpiredError("logged out")

    def messages(self):
        return [("user", "hi")]

    def has_request(self, request_id):
        return False

    def reconcile(self, request_id):
        raise AssertionError("doctor must never reconcile (it would reload)")

    def submit(self, request_id, prompt):
        self.submits.append((request_id, prompt))

    def await_response(self, request_id):
        raise AssertionError("doctor must never await a response")

    def close(self):
        self.closed = True


def make_config(tmp_path, provider=None, codex=None, **policy) -> AutoloopConfig:
    # `tmp_path` doubles as `repo_root` in every `run_doctor(...)` call in
    # this file, so `workers_root` must be a SIBLING of it (never a child) —
    # matching the pattern `test_full_healthy_run` already uses for the
    # `origin` bare repo below.
    conversation = (
        ConversationConfig(provider=provider) if provider else ConversationConfig()
    )
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(**policy),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path.parent / f"{tmp_path.name}-workers_root",
        conversation=conversation,
        codex=codex or CodexConfig(),
    )


def preflight_ok(detail="`codex exec` ran in a fake directory"):
    """A stand-in for the codex preflight, defaulting to the healthy answer."""
    return PreflightResult("ok", detail, preflight.OK)


def probes(conversation=None, codex_preflight=None) -> DoctorProbes:
    """`probe_cdp`/`playwright_present` were arguments here until brw-19c and
    are gone with the fields they set: a helper that still accepted them would
    let a test believe it had stubbed a check that no longer exists.

    `codex_preflight` is stubbed by DEFAULT, and that default is not a
    convenience: `make_config` configures a `codex_cli` seat, so an unstubbed
    bundle would launch a real `codex exec` — spending the operator's ChatGPT
    allowance — in every test in this file, on any machine that has the binary,
    and would fail everywhere else. Tests about the preflight's own logic live
    in `test_codex_preflight.py`, where the invocation boundary is faked one
    level down."""
    return DoctorProbes(
        conversation_factory=(lambda: conversation) if conversation else None,
        codex_preflight=codex_preflight or (lambda codex: preflight_ok()),
    )


def by_name(results):
    return {r.name: r for r in results}


def init_repo(path):
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "feature/x", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.c"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "commit.gpgsign=false", "commit", "-qm", "i"],
        check=True,
    )


def test_all_green(tmp_path):
    import subprocess

    init_repo(tmp_path)
    # A configured `origin` is what makes the publisher/worker-isolation
    # checks (added 2026-07-30) able to report "ok" at all — a repo with no
    # push destination legitimately fails `publisher` (nothing to snapshot).
    origin = tmp_path.parent / f"{tmp_path.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", str(origin)], check=True
    )
    conversation = FakeConversation()
    results = run_doctor(make_config(tmp_path), tmp_path, probes(conversation))
    named = by_name(results)
    for check in (
        "config", "state_dir", "lock", "workers_root", "worker_isolation", "hooks_dirs",
        "publisher", "publisher_url_drift", "provider", "primary_live",
    ):
        assert named[check].status == "ok", (check, named[check].detail)
    # THE brw-19c claim, on the run that is meant to be green: this machine has
    # no Chrome and no playwright, and the sweep says nothing about either.
    # Before brw-19c the `cdp` probe was a real HTTP dial and this same tree
    # exited 1 unless the operator's dedicated browser happened to be up.
    assert REMOVED_CHECKS.isdisjoint(named), sorted(REMOVED_CHECKS & set(named))
    assert exit_code(results) == 0
    # non-destructive: opened but NEVER submitted
    assert conversation.opened == 1
    assert conversation.submits == []
    assert conversation.closed


def test_git_identity_and_protected_branch_warning(tmp_path):
    import subprocess

    init_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "branch", "-m", "main"], check=True)
    results = run_doctor(make_config(tmp_path), tmp_path, probes(FakeConversation()))
    named = by_name(results)
    assert named["git"].status == "ok"
    assert "branch=main" in named["git"].detail
    assert named["branch_policy"].status == "warn"  # main is protected by default
    assert "DENIED" in named["branch_policy"].detail


# ---- the removed browser checks, pinned by their absence ---------------------


def test_the_retired_provider_gets_no_browser_checks_of_its_own(tmp_path):
    """A config still naming `browser_chatgpt` LOADS (that compatibility is
    `test_browser_provider_removed.py`'s subject) and reaches doctor. What it
    must not reach is a URL-shape check, a rotation report or a rotation
    target: those were keyed on this literal name, and a name is not a
    capability. The config below still carries a full-looking browser URL, so
    the absence is about the CHECKS and not about there being nothing to
    check."""
    results = run_doctor(
        make_config(tmp_path, provider=RETIRED_BROWSER_PROVIDER),
        tmp_path,
        probes(FakeConversation()),
    )
    named = by_name(results)

    assert REMOVED_CHECKS.isdisjoint(named), sorted(REMOVED_CHECKS & set(named))
    assert named["provider"].status == "fail", "it is still an unregistered name"


def test_an_unbuildable_seat_fails_rather_than_skipping(tmp_path):
    """The fail-open the removed `skip` was: `primary_live` reported `skip`
    for `browser_chatgpt` whenever CDP or playwright was unavailable, so the
    ONE check that actually opens a transport answered "not asked" on exactly
    the machines where the answer mattered. With no probes to consult, the
    real factory is used and its refusal is reported as the failure it is.

    Runs with NO `conversation_factory`, deliberately: a stubbed factory would
    prove the assertion about the stub."""
    results = run_doctor(
        make_config(tmp_path, provider=RETIRED_BROWSER_PROVIDER), tmp_path, probes()
    )
    named = by_name(results)

    assert named["primary_live"].status == "fail"
    assert RETIRED_BROWSER_PROVIDER in named["primary_live"].detail
    assert "unknown conversation provider" in named["primary_live"].detail
    assert exit_code(results) == 1


def test_a_leftover_browser_section_is_accepted_and_never_graded(tmp_path):
    """The compatibility half, from doctor's side. Every `[browser]` value an
    unmigrated config can still carry — including ones the removed checks
    would have called malformed — passes through the sweep without producing
    a single result about them."""
    config = AutoloopConfig(
        browser=BrowserConfig(
            conversation_url="https://chatgpt.com/c/REPLACE-ME",  # the old placeholder failure
            project_url="not-a-project-url-at-all",  # the old project_url failure
            cdp_url="http://127.0.0.1:1",  # nothing has ever listened here
            restart_command=("/path/to/your-restart-command",),
        ),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path.parent / f"{tmp_path.name}-workers_root",
    )

    results = run_doctor(config, tmp_path, probes(FakeConversation()))
    named = by_name(results)

    # The sweep RAN, asserted before anything is asserted about what it did not
    # produce: all three checks below are over `results`, and all three are
    # vacuously true of an empty one. "Never graded" has to mean the sweep
    # looked and said nothing, not that there was no sweep.
    assert named["config"].status == "ok", named["config"].detail
    assert "primary_live" in named, sorted(named)
    assert REMOVED_CHECKS.isdisjoint(named), sorted(REMOVED_CHECKS & set(named))
    assert not [r for r in results if "REPLACE-ME" in r.detail]
    assert not [r for r in results if "127.0.0.1:1" in r.detail]


def test_the_probe_bundle_no_longer_offers_a_browser_knob(tmp_path):
    """Asserted on the dataclass rather than on a sweep: a `DoctorProbes` that
    still ACCEPTED `probe_cdp` would let `cli._precondition_transport_live` and
    every future caller believe they were injecting a check that no longer runs
    — a stub with nothing behind it is the quietest kind of dead guard."""
    fields = {f.name for f in dataclasses.fields(DoctorProbes)}

    # `codex_preflight` (prov-02) is the second boundary, and it is a REAL one:
    # the default launches a codex process. The assertion stays exact so a
    # knob nothing reads still cannot be added quietly.
    assert fields == {"conversation_factory", "codex_preflight"}
    with pytest.raises(TypeError):
        DoctorProbes(probe_cdp=lambda url: "{}")


# ---- the rows `cli`'s transport precondition depends on ----------------------


def test_doctor_emits_every_required_transport_precondition_row(tmp_path):
    """`cli._precondition_transport_live` REFUSES to clear `login_expired`,
    `submission_ambiguous` or `browser_unattachable` unless every name in
    `cli._TRANSPORT_PRECONDITION_CHECKS` came back from a real sweep. That is
    the fail-closed direction, but it is only useful if doctor actually emits
    those rows — a required name doctor never produces would refuse those
    blockers forever, with no operator action able to clear them.

    So the coverage is DERIVED from the tuple and from a live `run_doctor`,
    never restated as a literal list here. Removing `primary_live` or `provider`
    from doctor fails here; renaming one and updating only cli fails here too.
    The sweep runs against a repository with no browser and no network, which
    is the deployment shape brw-19c made the normal one."""
    from autoloop.cli import (
        _TRANSPORT_PRECONDITION_CHECKS,
        _TRANSPORT_PRECONDITION_FAIL_ONLY_CHECKS,
    )

    init_repo(tmp_path)
    results = run_doctor(make_config(tmp_path), tmp_path, probes(FakeConversation()))
    named = by_name(results)

    assert _TRANSPORT_PRECONDITION_CHECKS, "an empty required tuple grades nothing"
    missing = set(_TRANSPORT_PRECONDITION_CHECKS) - set(named)
    assert not missing, f"required rows doctor does not emit: {sorted(missing)}"

    # The fail-only tier is unconditional too — `fallback_live` is added on
    # every branch of check 14, including the no-fallback one this config takes
    # — so a name there that doctor cannot produce is the same dead guard,
    # merely quieter about it.
    unproducible = set(_TRANSPORT_PRECONDITION_FAIL_ONLY_CHECKS) - set(named)
    assert not unproducible, f"fail-only rows doctor does not emit: {sorted(unproducible)}"
    assert named["fallback_live"].status == "warn", (
        "the no-fallback default must be WARN — if doctor ever made it `fail`, "
        "every single-seat deployment's transport blockers would jam shut"
    )


def test_doctor_emits_the_optional_transport_rows_when_their_seat_is_configured(tmp_path):
    """The optional half, pinned for the reason `cdp` and `playwright` are a
    cautionary tale: a name graded "only when present" is indistinguishable
    from a name that can never be present, and the second one grades nothing
    while looking exactly like the first.

    `codex_command` is emitted by `run_doctor` only for a codex seat, so the
    config here configures one. `_probe_live` is stubbed, so this asserts the
    ROW exists — not that any binary does."""
    from autoloop.cli import _TRANSPORT_PRECONDITION_OPTIONAL_CHECKS

    results = run_doctor(
        make_config(tmp_path, provider="codex_cli"), tmp_path, probes(FakeConversation())
    )
    named = by_name(results)

    missing = set(_TRANSPORT_PRECONDITION_OPTIONAL_CHECKS) - set(named)
    assert not missing, (
        "optional rows no configuration can produce are dead weight, not "
        f"corroboration: {sorted(missing)}"
    )


def test_the_unregistered_provider_row_is_the_one_the_precondition_reads(tmp_path):
    """Provider registration is a SEPARATE signal from the live probe, and the
    precondition treats it as one. The evidence it reads is this row, produced
    here by a real sweep over a config naming the retired provider: `fail`,
    under the name `cli._TRANSPORT_PRECONDITION_CHECKS` demands.

    Pinned in doctor rather than only in `test_blockers.py` because that file
    stubs the sweep — a stub proves the CLI honours a verdict, never that
    doctor still produces one."""
    from autoloop.cli import _TRANSPORT_PRECONDITION_CHECKS

    results = run_doctor(
        make_config(tmp_path, provider=RETIRED_BROWSER_PROVIDER),
        tmp_path,
        probes(FakeConversation()),
    )
    named = by_name(results)

    assert "provider" in _TRANSPORT_PRECONDITION_CHECKS
    assert named["provider"].status == "fail"
    assert RETIRED_BROWSER_PROVIDER in named["provider"].detail


def test_logged_out_provider_fails(tmp_path):
    conversation = FakeConversation(login_expired=True)
    results = run_doctor(make_config(tmp_path), tmp_path, probes(conversation))
    named = by_name(results)
    assert named["primary_live"].status == "fail"
    assert "logged out" in named["primary_live"].detail
    assert conversation.closed


def test_stale_lock_reported_as_failure(tmp_path):
    config = make_config(tmp_path)
    lock = LoopLock(config.state_dir)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps(
            {
                "pid": 99999999,
                "hostname": socket.gethostname(),
                "started_at": "t",
                "run_id": "r",
                "state_dir": str(config.state_dir),
            }
        ),
        encoding="utf-8",
    )
    results = run_doctor(config, tmp_path, probes(FakeConversation()))
    named = by_name(results)
    assert named["lock"].status == "fail"
    assert "unlock" in named["lock"].detail


# ---- the codex seat: command, confinement and the preflight (prov-02) --------


def codex_rows(tmp_path, codex=None, codex_preflight=None, provider="codex_cli"):
    results = run_doctor(
        make_config(tmp_path, provider=provider, codex=codex),
        tmp_path,
        probes(FakeConversation(), codex_preflight=codex_preflight),
    )
    return by_name(results), results


def codex_exit(named, *names):
    """`exit_code` over the named rows ALONE.

    `tmp_path` here is not a git repository and `codex` may not be installed,
    so a whole-sweep `exit_code` would be 1 for reasons that have nothing to do
    with the claim under test — and 1 for the right reason would then be
    indistinguishable from 1 for the wrong one."""
    return exit_code([named[name] for name in names])


def test_doctor_distinguishes_a_resolvable_codex_command_from_an_unresolvable_one(
    tmp_path, monkeypatch
):
    """`codex_command` is the cheapest of the four rows and the one every other
    check depends on: an unresolvable command means the preflight below has
    nothing to invoke.

    PATH is stubbed both ways rather than read from the machine, so this says
    something about `doctor` instead of about whether the developer happens to
    have codex installed — which is what the row's only previous coverage (that
    it EXISTS) left open."""
    monkeypatch.setattr(doctor.shutil, "which", lambda binary: f"/fake/bin/{binary}")
    named, _ = codex_rows(tmp_path)
    assert named["codex_command"].status == "ok"
    assert "/fake/bin/codex" in named["codex_command"].detail
    assert codex_exit(named, "codex_command") == 0

    monkeypatch.setattr(doctor.shutil, "which", lambda binary: None)
    named, _ = codex_rows(tmp_path)
    assert named["codex_command"].status == "fail"
    assert "not on PATH" in named["codex_command"].detail
    assert codex_exit(named, "codex_command") == 1


def test_the_preflight_row_carries_the_probe_verdict_and_is_not_attempted_blindly(tmp_path):
    """The row that answers "could this loop use codex_cli right now".

    Both halves in one test because they are one claim: `doctor` reports what
    the preflight said, and it only asks when asking is safe."""
    named, _ = codex_rows(tmp_path)
    assert named["codex_preflight"].status == "ok", named["codex_preflight"].detail
    assert codex_exit(named, "codex_preflight") == 0

    # A refused invocation is the row's `fail`, and the detail is the probe's.
    def refused(codex):
        return PreflightResult(
            "fail",
            "`codex exec` exited 1: Not inside a trusted directory",
            preflight.INVOCATION_FAILED,
        )

    named, _ = codex_rows(tmp_path, codex_preflight=refused)
    assert named["codex_preflight"].status == "fail"
    assert "trusted directory" in named["codex_preflight"].detail
    assert codex_exit(named, "codex_preflight") == 1


def test_a_spent_allowance_is_a_warning_and_never_a_failed_check(tmp_path):
    """An account state is not a misconfiguration. `doctor` exiting 1 because
    the weekly window is used up would tell an operator to fix a machine that
    is correctly configured — and, through `cli._precondition_*`, is the shape
    that holds blockers shut on a condition no operator action can clear."""

    def spent(codex):
        return PreflightResult(
            "warn",
            "the codex allowance is SPENT: codex said 'usage limit reached'",
            preflight.QUOTA_EXHAUSTED,
        )

    named, _ = codex_rows(tmp_path, codex_preflight=spent)

    assert named["codex_preflight"].status == "warn"
    assert "usage limit reached" in named["codex_preflight"].detail
    assert codex_exit(named, "codex_preflight") == 0


def test_a_working_dir_inside_the_repository_is_refused_before_anything_runs(tmp_path):
    """The one case where NOT asking is the right answer: running the reviewer
    inside the checkout to find out whether it runs is doing the thing being
    diagnosed. `fail`, never `skip` — "not attempted" is not evidence that the
    transport works, which is exactly what the removed `cdp` skip claimed."""
    asked = []

    def probe(codex):
        asked.append(codex)
        return preflight_ok()

    named, _ = codex_rows(
        tmp_path,
        codex=CodexConfig(working_dir=str(tmp_path / "inside")),
        codex_preflight=probe,
    )

    assert named["codex_workdir"].status == "fail"
    assert "INSIDE the repository" in named["codex_workdir"].detail
    assert named["codex_preflight"].status == "fail"
    assert "not attempted" in named["codex_preflight"].detail
    assert asked == [], "the probe must not run the reviewer inside the checkout"
    assert codex_exit(named, "codex_workdir", "codex_preflight") == 1


def test_a_configured_working_dir_that_does_not_exist_fails_and_is_never_created(tmp_path):
    """A configured path is the operator's statement about where reviews
    happen. Creating a mistyped one would run them somewhere nobody named."""
    missing = tmp_path.parent / f"{tmp_path.name}-not-there"
    named, _ = codex_rows(tmp_path, codex=CodexConfig(working_dir=str(missing)))

    assert named["codex_workdir"].status == "fail"
    assert str(missing) in named["codex_workdir"].detail
    assert not missing.exists()
    assert named["codex_preflight"].status == "fail"


def test_the_default_path_spelled_out_is_graded_as_a_configured_directory(
    tmp_path, monkeypatch
):
    """The row and the reviewer must agree about WHO owns the directory, and the
    only way they can disagree is if one of them decides it by comparing paths.

    `codex.working_dir = "~/.autoloop/codex-workdir"` resolves to the same place
    as an unset one, and it is still the operator's statement — so `doctor`
    fails it while absent rather than promising it is "created on first use",
    which is what the reviewer's own ensure step would then refuse. `$HOME` is
    moved to a sibling of the repository so the default lands outside the
    checkout and this grades provenance rather than the INSIDE-the-repo rule."""
    home = tmp_path.parent / f"{tmp_path.name}-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    default = default_working_dir()
    assert not default.exists()
    asked = []

    def probe(codex):
        asked.append(codex)
        return preflight_ok()

    named, _ = codex_rows(
        tmp_path, codex=CodexConfig(working_dir=str(default)), codex_preflight=probe
    )

    assert named["codex_workdir"].status == "fail"
    assert str(default) in named["codex_workdir"].detail
    assert not default.exists(), "and doctor creates nothing on the way to saying so"
    assert named["codex_preflight"].status == "fail"
    assert "not attempted" in named["codex_preflight"].detail
    assert asked == []
    assert codex_exit(named, "codex_workdir", "codex_preflight") == 1

    # The same absolute path, left UNSET: autoloop's own, and reported as about
    # to be created rather than as a fault.
    named, _ = codex_rows(tmp_path, codex=CodexConfig(), codex_preflight=probe)

    assert named["codex_workdir"].status == "ok"
    assert "created on first use" in named["codex_workdir"].detail
    assert codex_exit(named, "codex_workdir", "codex_preflight") == 0
    assert len(asked) == 1, "and only the usable directory is preflighted"


def test_the_graded_working_dir_is_the_one_the_reviewer_actually_gets(tmp_path):
    """The defect this whole check is downstream of was a doctor row that
    passed about a directory nothing used. Both sides resolve through
    `preflight.resolve_working_dir`, so this asserts the identity rather than
    two copies of a rule."""
    from autoloop.codex.conversation import SubprocessCodexRunner

    named, _ = codex_rows(tmp_path)
    assert str(default_working_dir()) in named["codex_workdir"].detail
    assert SubprocessCodexRunner()._cwd == default_working_dir()

    configured = tmp_path.parent / f"{tmp_path.name}-reviews"
    configured.mkdir()
    named, _ = codex_rows(tmp_path, codex=CodexConfig(working_dir=str(configured)))
    assert named["codex_workdir"].status == "ok"
    assert str(configured) in named["codex_workdir"].detail
    assert SubprocessCodexRunner(cwd=configured)._cwd == configured


def test_the_shipped_sandbox_policy_passes_and_is_named_in_the_row(tmp_path):
    """`codex_sandbox` is the confinement row. The default seat is confined, so
    it reads `ok` — and it says what the policy does and does not do, because a
    row an operator reads as "the reviewer cannot see the checkout", or as "the
    reviewer cannot run anything", is a guarantee codex does not give stated
    where the seat is selected.

    Both halves are asserted HERE and not only in `test_codex_preflight.py`:
    this is the text an operator actually meets, and doctor composes it
    (`policy.detail` plus its own sentence) rather than printing it through."""
    named, _ = codex_rows(tmp_path)
    row = named["codex_sandbox"]

    assert row.status == "ok"
    assert "--sandbox read-only" in row.detail
    # What read-only really does: restricts writes, confines neither reads nor
    # command execution.
    assert "WRITES are restricted" in row.detail
    assert "does NOT confine READS" in row.detail
    assert "commands still run" in row.detail
    assert "command execution are refused" not in row.detail
    assert codex_exit(named, "codex_sandbox") == 0


def test_an_unconfined_seat_fails_and_the_preflight_is_not_attempted(tmp_path):
    """The claim: selecting `codex_cli` with no sandbox policy is not safe, and
    `doctor` says so BEFORE a round rather than a review saying so during one.

    The empty value is the one every config carried until this round, and it was
    reported as a deliberate policy — `warn`, exit 0 — on the argument that
    confinement rested on `codex.working_dir`. A working directory confines
    nothing, so this is a `fail`; and the probe is not asked, because answering
    "is this seat safe" by launching an unsandboxed reviewer answers a different
    question."""
    asked = []

    def probe(codex):
        asked.append(codex)
        return preflight_ok()

    named, _ = codex_rows(
        tmp_path, codex=CodexConfig(sandbox_args=()), codex_preflight=probe
    )

    assert named["codex_sandbox"].status == "fail"
    assert "UNCONFINED" in named["codex_sandbox"].detail
    assert named["codex_preflight"].status == "fail"
    assert "not attempted" in named["codex_preflight"].detail
    assert asked == [], "an unsandboxed reviewer must not be launched to grade itself"
    assert codex_exit(named, "codex_sandbox", "codex_preflight") == 1


@pytest.mark.parametrize(
    "args, status",
    [
        (("--sandbox", "workspace-write"), "warn"),
        (("--dangerously-bypass-approvals-and-sandbox",), "fail"),
        (("--sandbox", "banana"), "fail"),
    ],
)
def test_the_row_grades_the_policy_rather_than_whether_anything_is_set(
    tmp_path, args, status
):
    """Whether the list is SET is not the question. A bypass flag and a mode the
    loop cannot name are both a seat with no sandbox, and both were `ok` under
    the old row, which only tested that the list was non-empty."""
    named, _ = codex_rows(tmp_path, codex=CodexConfig(sandbox_args=args))

    assert named["codex_sandbox"].status == status


def test_a_preflight_that_raises_or_answers_nonsense_is_a_failure_not_a_pass(tmp_path):
    """Both fail-open shapes at the seam `doctor` cannot see inside.

    A probe that raises would take the whole sweep with it if it were not
    caught, and a status `exit_code` does not recognise would be counted as a
    pass — `exit_code` only looks for the literal `"fail"`."""

    def exploding(codex):
        raise RuntimeError("no PATH at all")

    named, _ = codex_rows(tmp_path, codex_preflight=exploding)
    assert named["codex_preflight"].status == "fail"
    assert "RuntimeError" in named["codex_preflight"].detail
    assert codex_exit(named, "codex_preflight") == 1

    def nonsense(codex):
        return PreflightResult("green", "everything is fine, honestly", preflight.OK)

    named, _ = codex_rows(tmp_path, codex_preflight=nonsense)
    assert named["codex_preflight"].status == "fail"
    assert "unrecognised status" in named["codex_preflight"].detail
    assert codex_exit(named, "codex_preflight") == 1


def test_no_codex_rows_at_all_when_no_codex_seat_is_configured(tmp_path):
    """The rows are emitted for a `codex_cli` seat and for nothing else — a
    check that grades a transport the config does not name is the `cdp`
    mistake, and this one would launch a process to do it."""
    asked = []
    named, _ = codex_rows(
        tmp_path,
        provider=RETIRED_BROWSER_PROVIDER,
        codex_preflight=lambda codex: asked.append(codex) or preflight_ok(),
    )

    assert not {"codex_command", "codex_workdir", "codex_sandbox", "codex_preflight"} & set(
        named
    ), sorted(named)
    assert asked == []


#: The URL-shape checks that lived here — five accepted forms, five rejected
#: ones and the `REPLACE-ME` placeholder — went with `doctor.py`'s
#: `_CHATGPT_URL` / `_CHATGPT_PROJECT_URL` regexes in brw-19c. They graded
#: `browser.conversation_url` for the retired `browser_chatgpt` seat only, and
#: nothing navigates to that value any more. Their replacement is
#: `test_the_retired_provider_gets_no_browser_checks_of_its_own` and
#: `test_a_leftover_browser_section_is_accepted_and_never_graded` above: the
#: claim is no longer "this URL is well formed" but "no URL is judged, and a
#: config carrying a malformed one still loads and still passes".
