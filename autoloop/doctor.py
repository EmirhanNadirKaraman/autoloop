"""`python -m autoloop doctor` — non-destructive preflight.

Checks configuration, state dir, lock, git identity, branch policy, worker
isolation, controlled hooks directories, publisher configuration, publisher
URL snapshot drift, provider registration, the Codex reviewer's binary,
confinement and one real trivial invocation, and that each configured seat's
adapter opens. It NEVER touches the reviewer conversation: nothing here
submits a review, reads one back, or reconciles one.

THE ONE PROCESS IT STARTS, since prov-02 (2026-09-01), is the codex preflight
— `codex.preflight.preflight_codex`, a fixed one-line prompt run from the
configured working directory UNDER THE CONFIGURED SANDBOX POLICY, bounded by
its own short deadline. That is not the reviewer conversation and carries no
review packet, and it is here because the question "could this loop use
codex_cli right now" was previously answered by two checks that could both pass
on a machine where every review would fail: the shipped `working_dir` default
was the home directory, and codex refuses to run there.

It is not started at all when `codex.sandbox_args` names no enforceable sandbox
(`codex_sandbox` fails first): the question "is this seat safe to select" must
never be answered by launching an unsandboxed reviewer.

NO BROWSER CHECKS since brw-19c (2026-08-31). Until then this command probed
the CDP endpoint, imported playwright, and — for the literal provider name
`browser_chatgpt` — checked a conversation URL's shape, the rotation budget
and the rotation target. brw-16 (2026-08-25) unregistered that provider, so
every one of those branches was unreachable on a real deployment, and the two
unconditional probes FAILED (exit 1) on any machine without a dedicated Chrome
— a preflight that cries wolf about a transport the loop cannot select is worse
than no preflight, because operators learn to ignore its exit code.

"Non-destructive" means never irreversible and never touching the real
conversation or the target repo's own history — it does create/remove a
throwaway probe worker repo and idempotently provision the publisher repo,
both scoped entirely under `config.state_dir` (autoloop's own scratch space),
the same category of side effect as the existing state-dir-writable probe
file.

Every external boundary is injectable (DoctorProbes) so the whole command is
unit-testable without a network.
"""

from __future__ import annotations

import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .codex.preflight import (
    PREFLIGHT_STATUSES,
    default_working_dir,
    preflight_codex,
    resolve_working_dir,
)
from .codex.sandbox import describe_invocation
from .config import AutoloopConfig
from .conversation import available_providers, create_conversation
from .errors import AutoloopError, BrowserError, LoginExpiredError
from .git_gateway import GitGateway
from .lock import LoopLock
from .policy import PolicyEngine
from .publisher import (
    Publisher,
    provision_publisher_repo,
    publisher_hooks_path,
    read_publisher_url_snapshot,
    redact_url,
)
from .validation_env import load_validation_env, validate_validation_env_path
from .worker_env import WorkerRepoManager, validate_workers_root, verify_worker_isolation


def _is_within(candidate: Path, root: Path) -> bool:
    """True when `candidate` resolves inside `root`.

    Resolves both sides first, so a symlink pointing into the checkout is
    caught rather than passing a string comparison.
    """
    try:
        Path(candidate).expanduser().resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


def _probe_live(add, name, provider_name, config, probes):
    """Open one provider's conversation read-only and report what resolved.

    Never submits. Each seat is probed independently, so a fault in one is
    never read as a verdict on the other.

    Nothing is SKIPPED here any more (brw-19c): the one skip this had was for
    the retired `browser_chatgpt` seat when CDP or playwright was unavailable,
    and both of those probes are gone with it. A provider whose prerequisites
    are missing now FAILS with the adapter's own error, which is the honest
    answer for a transport the loop would really try to build.
    """
    factory = probes.conversation_factory
    if factory is None:

        def factory():
            return create_conversation(provider_name, config)

    conversation = None
    try:
        conversation = factory()
        # attach() navigates only if the page is elsewhere, then waits for the
        # composer and checks login. It never types and never submits. For a
        # CLI provider it is an honest no-op, so this reports reachability of
        # the adapter, not of the binary — see the `codex_command` check.
        conversation.attach()
        messages = getattr(conversation, "messages", None)
        if callable(messages):
            count = len(messages())
            add(
                name,
                "ok",
                f"{provider_name}: logged in; conversation open; composer + "
                f"message selectors resolve ({count} messages visible)",
            )
        else:
            add(name, "ok", f"{provider_name}: constructed (no message probe exposed)")
    except LoginExpiredError as exc:
        add(name, "fail", f"{provider_name}: logged out: {exc}")
    except (BrowserError, AutoloopError) as exc:
        add(name, "fail", f"{provider_name}: {exc}")
    finally:
        if conversation is not None:
            try:
                conversation.close()
            except Exception:
                pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str


def _default_probe_cdp(url: str, timeout: float = 3.0) -> str:
    """A one-shot read of a CDP endpoint.

    `doctor` stopped calling this in brw-19c (2026-08-31) — see the module
    docstring. It stays HERE, rather than moving or going away, because
    `cli._repair_browser` imports it by this name from this module and
    `cli._default_probe_cdp` is the seam `start`'s tests patch. `start` may
    still restart an operator-declared browser; the preflight no longer
    grades one.
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local CDP
        return response.read(200).decode("utf-8", "replace")


@dataclass
class DoctorProbes:
    #: `probe_cdp` and `playwright_present` were fields here until brw-19c and
    #: are gone with the checks that read them: a probe nothing calls is a knob
    #: that suggests a check exists.
    conversation_factory: Callable | None = None  # defaults to the real provider
    #: The codex preflight (prov-02). Injectable because the default LAUNCHES A
    #: REAL BINARY from a real directory — the one boundary in this file that
    #: costs an invocation — and the suite must never do that: codex is not
    #: installed everywhere the tests run, and where it is, a doctor test would
    #: spend the operator's allowance. Takes a `CodexConfig`, returns a
    #: `preflight.PreflightResult`.
    codex_preflight: Callable | None = None  # defaults to preflight_codex


def run_doctor(
    config: AutoloopConfig, repo_root: Path, probes: DoctorProbes | None = None
) -> list[CheckResult]:
    probes = probes or DoctorProbes()
    results: list[CheckResult] = []

    def add(name: str, status: str, detail: str) -> None:
        results.append(CheckResult(name=name, status=status, detail=detail))

    # 1. config already loaded by the caller — record it.
    add("config", "ok", "parsed; unknown keys would have been rejected at load")

    # 2. state dir writable
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        probe = config.state_dir / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("state_dir", "ok", str(config.state_dir))
    except OSError as exc:
        add("state_dir", "fail", f"not writable: {exc}")

    # 3. lock availability
    lock = LoopLock(config.state_dir)
    info = lock.read()
    if info is None:
        add("lock", "ok", "free")
    elif LoopLock.is_live(info):
        add("lock", "warn", f"held by a live process ({info.describe()})")
    else:
        add(
            "lock",
            "fail",
            f"stale lock ({info.describe()}) — recover with `python -m autoloop unlock`",
        )

    # 3b. workers_root — Autoloop M1 finding #1. MUST be absolute and
    # entirely outside the checkout/.git/state dir/publisher dirs; refuses
    # (never falls back to the old `config.workers_dir`) if not. This gates
    # check 6 below: creating even a THROWAWAY probe repo at an unsafe
    # location would be doing exactly the unsafe thing being diagnosed.
    workers_root_violations = validate_workers_root(config.workers_root, repo_root, config.state_dir)
    if workers_root_violations:
        add("workers_root", "fail", "; ".join(workers_root_violations))
    else:
        add("workers_root", "ok", str(Path(config.workers_root).resolve()))

    # 3b-bis. validation environment (the credential boundary). Reports the
    # SAME checks `cli._load_validation_env` enforces, so doctor can never
    # come back clean on a configuration a real run would refuse. Names and
    # paths only — `ValidationEnv` has no accessor that yields a value, and
    # its `describe()` says so explicitly.
    if config.validation_env_file is None:
        add(
            "validation_env",
            "skip",
            "no paths.validation_env_file configured — post-writer validation "
            "runs with no database credentials (correct unless a task's declared "
            "validation needs one)",
        )
    else:
        location_violations = validate_validation_env_path(
            config.validation_env_file, repo_root, config.state_dir, config.workers_root
        )
        if location_violations:
            add("validation_env", "fail", "; ".join(location_violations))
        else:
            try:
                loaded = load_validation_env(
                    config.validation_env_file,
                    repo_root=repo_root,
                    state_dir=config.state_dir,
                    workers_root=config.workers_root,
                    # Same `[repo]` values `cli._load_validation_env` passes.
                    # Defaulting either of them here would reintroduce exactly
                    # the doctor/run divergence this block's comment forbids.
                    env_example_file=config.repo.env_example_file,
                    env_example_db_key=config.repo.env_example_db_key,
                )
            except AutoloopError as exc:
                add("validation_env", "fail", str(exc))
            else:
                add(
                    "validation_env",
                    "ok",
                    f"{loaded.path} defines {', '.join(loaded.keys())} "
                    "(values redacted; delivered only to the post-writer "
                    "validation subprocess, stripped from the agent subprocess)",
                )

    # 3c. legacy workers — pre-M1-fix deployments created worker repos under
    # `config.workers_dir` (`state_dir / "workers"`, nested inside the
    # checkout). This never MOVES anything (moving repository changes
    # implicitly is explicitly refused by the brief this closes) — it only
    # reports what is there so an operator can migrate or discard it by
    # hand. `warn`, never `fail`: mere presence is not itself unsafe once
    # `workers_root` (above) is the location new work actually uses.
    legacy_root = config.workers_dir
    if legacy_root.is_dir():
        stray = []
        for task_dir in sorted(p for p in legacy_root.iterdir() if p.is_dir()):
            try:
                legacy_git = GitGateway(task_dir, PolicyEngine(config.policy))
                state_desc = "dirty working tree" if legacy_git.dirty_files() else "clean working tree"
                stray.append(f"{task_dir.name} ({state_desc}, head={legacy_git.head_sha()[:12]})")
            except AutoloopError:
                stray.append(f"{task_dir.name} (not a readable git repo)")
        if stray:
            add(
                "legacy_workers",
                "warn",
                f"{len(stray)} pre-M1 worker repo(s) still at {legacy_root} — "
                "never moved automatically, inspect and migrate/discard by hand: "
                + "; ".join(stray),
            )
        else:
            add("legacy_workers", "ok", f"{legacy_root} exists but holds no task directories")
    else:
        add("legacy_workers", "ok", f"{legacy_root} does not exist — nothing to migrate")

    # 4./5. git identity + branch policy
    git = GitGateway(repo_root, PolicyEngine(config.policy))
    try:
        branch = git.current_branch()
        head = git.head_sha()
        add("git", "ok", f"branch={branch} head={head[:12]} dirty={len(git.dirty_files())}")
        if branch in config.policy.protected_branches and not config.policy.allow_protected_push:
            add(
                "branch_policy",
                "warn",
                f"'{branch}' is protected — pushes will be DENIED "
                "(policy.protected_branches / allow_protected_push)",
            )
        else:
            add("branch_policy", "ok", f"pushes to '{branch}' are permitted by policy")
    except AutoloopError as exc:
        add("git", "fail", str(exc))

    # 6. worker isolation — create a throwaway probe worker repo at the
    # (validated, external) `workers_root`, verify it with the SAME
    # `verify_worker_isolation` production code uses, remove it. Proves the
    # WorkerRepoManager pipeline actually isolates today, not just that the
    # code reads correctly. Skipped (not "fail" — already reported by check
    # 3b) if `workers_root` itself is not safe to use.
    if workers_root_violations:
        add("worker_isolation", "skip", "workers_root is not valid — see the 'workers_root' check above")
    else:
        probe_id = "doctor-probe"
        worker_repos = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
        worker_repos.remove(probe_id)  # clear any stale probe from a crashed prior run
        try:
            head = GitGateway(repo_root, PolicyEngine(config.policy)).head_sha()
            worker = worker_repos.create(probe_id, repo_root, head)
            try:
                probe_git = worker.gateway(PolicyEngine(config.policy))
                violations = verify_worker_isolation(
                    probe_git, expected_hooks_dir=worker_repos.hooks_dir_for(probe_id)
                )
                if violations:
                    add("worker_isolation", "fail", "; ".join(violations))
                else:
                    add("worker_isolation", "ok", f"probe repo at {worker.path} is isolated")
            finally:
                worker_repos.remove(probe_id)
        except AutoloopError as exc:
            add("worker_isolation", "fail", str(exc))

    # 7. controlled hooks directories — every accumulated task's worker hooks
    # dir, and the publisher's own, must be empty. Broader than check 6: that
    # one probes a single fresh id; this sweeps every hooks dir that has ever
    # been created.
    try:
        stray = []
        if config.worker_hooks_dir.is_dir():
            for task_dir in sorted(config.worker_hooks_dir.iterdir()):
                if task_dir.is_dir() and any(task_dir.iterdir()):
                    stray.append(str(task_dir))
        pub_hooks = publisher_hooks_path(config.state_dir)
        if pub_hooks.is_dir() and any(pub_hooks.iterdir()):
            stray.append(str(pub_hooks))
        if stray:
            add("hooks_dirs", "fail", f"non-empty controlled hooks dir(s): {stray}")
        else:
            add("hooks_dirs", "ok", "all controlled hooks directories are empty")
    except OSError as exc:
        add("hooks_dirs", "fail", str(exc))

    # 8. publisher configuration — idempotently (re)provision (safe: scoped
    # entirely under state_dir, never touches the target repo), then
    # construct a real `Publisher` against it, which independently re-runs
    # the single-url / no-pushurl / no-mirror / no-followTags / no-insteadOf
    # / empty-hooks-dir checks `push_exact` itself relies on.
    try:
        git_for_publisher = GitGateway(repo_root, PolicyEngine(config.policy))
        publisher_path = provision_publisher_repo(config.state_dir, git_for_publisher)
        publisher = Publisher(publisher_path, "origin", PolicyEngine(config.policy))
        described = publisher.describe()
        add(
            "publisher",
            "ok",
            f"repo={described['repo_root']} url={described['remote_url_redacted']}",
        )
    except AutoloopError as exc:
        add("publisher", "fail", str(exc))

    # 9. publisher URL snapshot vs the CONFIGURED (main checkout's live)
    # remote.origin.url. A mismatch means the main checkout's origin changed
    # since the publisher was last (re)provisioned — `Orchestrator.
    # _dispatch_task_push` refuses to publish while this holds; the ONLY fix
    # is the explicit `reprovision-publisher --confirm` command named below.
    try:
        snapshot = read_publisher_url_snapshot(config.state_dir)
        live = GitGateway(repo_root, PolicyEngine(config.policy)).config_get(
            "remote.origin.url"
        )
        if snapshot is None:
            add(
                "publisher_url_drift",
                "warn",
                "no snapshot yet — provisioned by the `publisher` check above just now",
            )
        elif snapshot == live:
            add(
                "publisher_url_drift",
                "ok",
                f"snapshot matches configured remote.origin.url ({redact_url(live)})",
            )
        else:
            add(
                "publisher_url_drift",
                "fail",
                f"snapshot ({redact_url(snapshot)}) != configured remote.origin.url "
                f"({redact_url(live)}) — verify the new destination is correct, then "
                "run `python -m autoloop reprovision-publisher --confirm`",
            )
    except AutoloopError as exc:
        add("publisher_url_drift", "fail", str(exc))

    # 10./11. CDP endpoint and playwright — REMOVED in brw-19c (2026-08-31).
    # Both graded a browser stack no registered provider can select, and both
    # ran unconditionally, so `doctor` exited 1 on every machine without a
    # dedicated Chrome. See the module docstring.

    # 12. provider registration
    provider = config.conversation.provider
    if provider in available_providers():
        add("provider", "ok", provider)
    else:
        add("provider", "fail", f"'{provider}' not registered ({available_providers()})")

    # 13./13b./13c. conversation URL shape, the live conversation and the
    # rotation target — REMOVED in brw-19c (2026-08-31). All three were keyed
    # on the literal provider name `browser_chatgpt`, unregistered since brw-16
    # (2026-08-25), so no real deployment could reach any of them; the rotation
    # they reported on was itself removed by brw-15. `browser.conversation_url`
    # and `browser.project_url` are still ACCEPTED by the loader — see
    # `config.BrowserConfig` — they are simply no longer graded.

    # 13d. Codex reviewer, when either seat uses it. Four rows, and two of them
    # are the ones that answer the questions the others only circle. The live
    # probe below constructs the ADAPTER, `codex_command` resolves the BINARY,
    # `codex_workdir` grades a PATH — and all three passed on a machine where
    # every review would have failed, because the shipped `working_dir` default
    # was the home directory and codex declines to run there. `codex_sandbox`
    # answers "is this seat CONFINED" and `codex_preflight` answers "would a
    # review actually run"; either one failing means selecting codex_cli is not
    # safe, and the runner itself refuses the first of those at launch.
    codex_seats = {provider, config.conversation.fallback_provider} & {"codex_cli"}
    if codex_seats:
        binary = config.codex.command[0] if config.codex.command else ""
        found = shutil.which(binary) if binary else None
        if found:
            add("codex_command", "ok", f"{binary} -> {found}")
        else:
            add(
                "codex_command",
                "fail",
                f"'{binary}' is not on PATH — install the Codex CLI and sign in "
                "with `codex login`, or change codex.command",
            )
        # The SAME resolution the runners use, so this row can never grade a
        # directory the reviewer does not get (`codex.preflight`).
        workdir = resolve_working_dir(config.codex.working_dir)
        is_default = workdir == default_working_dir()
        inside_repo = _is_within(workdir, repo_root)
        exists = workdir.is_dir()
        if inside_repo:
            workdir_usable = False
            add(
                "codex_workdir",
                "fail",
                f"{workdir} — INSIDE the repository. The prompt is "
                "self-contained, so the reviewer has no business being STARTED "
                "in the tree it is grading; set codex.working_dir outside it. "
                "(This is not the confinement — that is codex.sandbox_args "
                "below. A working directory refuses nothing.)",
            )
        elif exists:
            add(
                "codex_workdir",
                "ok",
                f"{workdir} (outside the repository"
                + (", autoloop's dedicated directory" if is_default else ", configured")
                + ")",
            )
            workdir_usable = True
        elif is_default:
            # Absent and ours: the preflight below creates it, so this is a
            # statement about what is about to happen, not a fault.
            add(
                "codex_workdir",
                "ok",
                f"{workdir} does not exist yet — autoloop's dedicated reviewer "
                "directory, created on first use. Trust it once (`cd` there and "
                "run `codex`) or codex will refuse to run in it.",
            )
            workdir_usable = True
        else:
            workdir_usable = False
            add(
                "codex_workdir",
                "fail",
                f"codex.working_dir points at {workdir}, which does not exist. "
                "Create it, or correct the setting — a configured directory is "
                "never created for you, so a typo cannot become the place your "
                "reviews run.",
            )
        # THE confinement row. `codex.sandbox_args` is read as a POLICY
        # (`codex.sandbox.describe_invocation`), never as "set or not set": an
        # empty value, an unknown mode and a bypass flag are all a seat with no
        # sandbox, and all `fail`. This row used to `warn` that empty was the
        # shipped policy and that confinement rested on `codex.working_dir`
        # alone — which was not true of any working directory: `cwd` chooses
        # where a process starts and refuses nothing.
        # `command` too, not only `sandbox_args`: codex sees one argv, so a
        # bypass flag in the command line is a bypass, and a row that graded the
        # key MEANT to hold the policy would report `ok` about an invocation
        # that turns it off.
        policy = describe_invocation(config.codex.command, config.codex.sandbox_args)
        add(
            "codex_sandbox",
            policy.status,
            policy.detail
            + (
                " Passed to every invocation, including the preflight below."
                if policy.is_enforceable
                else ""
            ),
        )

        # 13e. THE preflight: one trivial invocation, from that directory, with
        # those flags. Not attempted when the directory is already refused
        # above — running the reviewer inside the checkout to find out whether
        # it runs would be doing the thing being diagnosed — nor when the
        # sandbox policy is not enforceable, for the same reason one level up:
        # the answer to "is this seat safe to select" must not be obtained by
        # launching an unsandboxed reviewer. `fail`, never `skip`, when it is
        # not attempted: "not asked" is not evidence that the transport works,
        # which is the fail-open the removed `cdp` skip was. `preflight_codex`
        # refuses both cases itself; this gate holds even when the probe is
        # replaced (`DoctorProbes.codex_preflight`).
        if not workdir_usable:
            add(
                "codex_preflight",
                "fail",
                "not attempted — the working directory is unusable; see the "
                "'codex_workdir' check above",
            )
        elif not policy.is_enforceable:
            add(
                "codex_preflight",
                "fail",
                "not attempted — the reviewer would run unsandboxed; see the "
                "'codex_sandbox' check above",
            )
        else:
            probe = probes.codex_preflight or preflight_codex
            try:
                outcome = probe(config.codex)
                status, detail = outcome.status, outcome.detail
            except Exception as exc:  # noqa: BLE001 - a sweep reports, never raises
                status, detail = (
                    "fail",
                    f"the codex preflight could not be run, so nothing was "
                    f"verified: {type(exc).__name__}: {exc}",
                )
            if status not in PREFLIGHT_STATUSES:
                # `exit_code` only looks for "fail", so an unrecognised status
                # would be counted as a pass. Refused rather than passed
                # through, and the original is quoted so the fault is visible.
                status, detail = (
                    "fail",
                    f"the codex preflight returned an unrecognised status "
                    f"{status!r}: {detail}",
                )
            add("codex_preflight", status, detail)

    # 14. live conversation checks: the adapter opens and reports. Never
    # submits.
    #
    # BOTH seats are probed, not just the configured primary. An unverified
    # fallback is not a fallback: checking only `conversation.provider` means
    # the other seat is first tested at the moment the allowance runs out —
    # the worst possible time to learn it stopped working three days ago.
    fallback = config.conversation.fallback_provider
    _probe_live(add, "primary_live", provider, config, probes)
    if fallback and fallback != provider:
        _probe_live(add, "fallback_live", fallback, config, probes)
    elif fallback == provider and fallback:
        add(
            "fallback_live",
            "warn",
            f"conversation.fallback_provider is also '{provider}' — a quota "
            "failover would have nowhere to go",
        )
    else:
        add(
            "fallback_live",
            "warn",
            "no conversation.fallback_provider configured — an exhausted "
            "allowance parks the loop instead of handing over",
        )
    return results


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == "fail" for r in results) else 0
