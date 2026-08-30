"""The loop runs the code it just merged, without an operator restarting it.

Measured 2026-08-18. The loop process started 04:07:03. plan-01 merged a hard
gate at 06:23:59 — "no task starts without an approved decomposition" — and at
09:00 the registry held 0 decompositions across 102 tasks, including dash-10,
which STARTED after the merge. Python loaded `policy.py` at 04:07; merging into
the checkout does not reload a live process. brw-11's throttle-vs-unattachable
fix, merged 00:58, was inert the same way all night. The loop could ship
improvements to itself that it then could not use.

The claim under test, in five parts, one per bound the design carries:

* a merge touching `autoloop/` re-execs at the next boundary,
* a docs-only merge does not,
* a merged tree that fails the preflight import does not exec, and the loop
  keeps running the old code,
* a re-exec that dies before one completed iteration is not retried,
* the LOCK is continuously valid across the replacement — and adoption is
  authorized by a token minted for that one `execv` and inherited only across
  it, so a marker left on disk proves nothing on its own.

**No test here replaces the pytest process.** `no_process_replacement` is
autouse and makes `os.execv` raise, so a test that reaches a real exec fails
loudly instead of turning the test session into a loop run; the two tests that
care about the exec install their own recorder over it. Same shape, and the
same reason, as `test_restart_wiring.py`'s `no_machine_access`.

Real git for the merge half (this package's convention — see
`test_auto_merge.py`), a real `LoopLock` on a `tmp_path` state dir for the lock
half, and a real subprocess for the preflight, since a preflight that does not
actually launch an interpreter proves nothing about the tree it is judging.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from gitrepo import make_repo_from_template

from autoloop import cli
from autoloop.auto_merge import (
    UPGRADE_DEFERRED,
    UPGRADE_EXEC_FAILED,
    UPGRADE_EXECED,
    UPGRADE_NONE,
    UPGRADE_PENDING,
    UPGRADE_PREFLIGHT_FAILED,
    UPGRADE_UNAPPLICABLE,
    PendingUpgrade,
    UpgradeStore,
    loop_code_paths,
    upgrade_bound_sha,
)
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import LockHeldError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.lock import (
    EXEC_HANDOFF_TOKEN_ENV,
    LOCK_FILENAME,
    LockInfo,
    LoopLock,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import SELF_UPGRADE, Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    LastResponse,
    LoopState,
    PendingRequest,
    Phase,
    StateStore,
    utcnow_iso,
)
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/self-upgrade"
BASE = "work"
REPO_ROOT = Path(__file__).resolve().parents[2]


class Execed(Exception):
    """Raised by the recorder standing in for `os.execv`. Deliberately not an
    `OSError`: the production code catches `OSError` as "the exec was refused"
    and would swallow it, so a test wanting to observe a REAL exec has to raise
    something that propagates."""


@pytest.fixture(autouse=True)
def no_process_replacement(monkeypatch):
    """Every route to replacing this process, disabled for the whole file.

    A test that actually called `os.execv` would replace the pytest process
    with a loop run — no failure, no report, just a test session that turns
    into something else. Pinned so a future test cannot do it by accident.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError("this file must never replace the pytest process")

    monkeypatch.setattr(os, "execv", refuse)


@pytest.fixture(autouse=True)
def no_inherited_handoff_token():
    """The pytest process inherited no handoff, and must not leave one behind.

    `mark_exec_handoff` writes a real environment variable in THIS process —
    that is how the token reaches the successor across `os.execv` — so a token
    minted by one test would otherwise still be set for the next, and a test
    asserting a refusal could pass on a leftover. Popped on both sides rather
    than via `monkeypatch.delenv`, which records nothing when the variable is
    absent (the usual case here) and so would not undo a token a test then set
    through the production path.
    """
    os.environ.pop(EXEC_HANDOFF_TOKEN_ENV, None)
    yield
    os.environ.pop(EXEC_HANDOFF_TOKEN_ENV, None)


def inherited_token() -> str | None:
    return os.environ.get(EXEC_HANDOFF_TOKEN_ENV)


def write_lock(tmp_path: Path, info: LockInfo, handoff: dict | None) -> None:
    """A lock file written by hand — the shape an attacker, a dead run or a
    stale state dir could leave behind, as opposed to one this process armed."""
    data = {
        "pid": info.pid,
        "hostname": info.hostname,
        "started_at": info.started_at,
        "run_id": info.run_id,
        "state_dir": info.state_dir,
    }
    if handoff is not None:
        data["exec_handoff"] = handoff
    (tmp_path / LOCK_FILENAME).write_text(json.dumps(data), encoding="utf-8")


def recording_execv(monkeypatch) -> list:
    """Install a recorder over `os.execv` and return the list it writes to.
    Raises `Execed` so the call site is provably not reached again."""
    calls: list = []

    def record(path, argv):
        calls.append((path, list(argv)))
        raise Execed(argv)

    monkeypatch.setattr(os, "execv", record)
    return calls


# --- small fixtures ----------------------------------------------------------


def make_config(tmp_path: Path) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
    )


def pending_record(**overrides) -> PendingUpgrade:
    data = {
        "base_sha": "b" * 40,
        "previous_base_sha": "a" * 40,
        "candidate_sha": "c" * 40,
        "task_id": "t1",
        "repo_root": str(REPO_ROOT),
        "paths": ["autoloop/policy.py"],
        "status": UPGRADE_PENDING,
        "recorded_at": utcnow_iso(),
    }
    data.update(overrides)
    return PendingUpgrade(**data)


def entries(config, entry_type=None) -> list:
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if entry_type is None or r["type"] == entry_type]


# --- the merge half: real git, real merge ------------------------------------
#
# Self-contained helpers per this package's test convention (see
# `test_postcommit_primitives.py`'s docstring for why they are duplicated
# rather than imported).


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class WritingExecutor:
    def __init__(self, worktrees_root, per_task):
        self.worktrees_root = Path(worktrees_root)
        self.per_task = {k: dict(v) for k, v in per_task.items()}

    def execute(self, directive, task):
        files = self.per_task[task.id]
        wt = self.worktrees_root / task.id
        for rel, content in files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary=f"wrote {sorted(files)}",
            details="details",
            validation="ok",
            changed_paths=tuple(files.keys()),
        )


class MergeHarness:
    """One real implement -> review -> approve -> push -> MERGE, so the record
    under test is written by the production path rather than by the test."""

    def __init__(self, orch, repo, config, execution_store):
        self.orch = orch
        self.repo = repo
        self.config = config
        self.execution_store = execution_store
        self.upgrades = UpgradeStore(config.pending_upgrade_file)

    def push(self, task_id):
        self.orch._dispatch_executor(
            Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)
        )
        self.orch._step_ready()
        req = self.orch.state.pending_request
        response = LastResponse(
            request_id=req.request_id, raw="{}", received_at="now",
            head_sha=req.head_sha, base_sha=req.base_sha,
            report_sha256=req.report_sha256, postcommit=req.postcommit,
        )
        self.orch._dispatch_task_push(
            Directive(decision=Decision.PUSH, reason="approved"), response
        )
        return self.execution_store.load(task_id)

    def head(self):
        return run_git(self.repo, "rev-parse", "HEAD").strip()


def build_merge(tmp_path, per_task):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_repo_from_template(repo, branch=BASE, files=(("README.md", "hello\n"),))

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "-u", "origin", BASE)

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True, auto_merge_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    git = GitGateway(repo, PolicyEngine(config.policy))
    tasks = [
        Task(id=tid, title=f"Title {tid}", description="desc",
             approved_paths=tuple(sorted(files)))
        for tid, files in per_task.items()
    ]
    registry = TaskRegistry(tasks)
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    execution_store = TaskExecutionStore(config.executions_dir)

    def no_client():
        raise AssertionError("no browser client expected in this test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=WritingExecutor(tmp_path / "worktrees", per_task),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worktrees=WorktreeManager(git, tmp_path / "worktrees"),
        execution_store=execution_store,
        intent_store=IntentStore(config.intents_dir),
        validation_runner=ok_validation,
    )
    return MergeHarness(orch, repo, config, execution_store)


def test_a_merge_that_changes_loop_code_records_the_sha_to_upgrade_to(tmp_path):
    """The signal, written by the real merge path. Everything downstream keys
    on `base_sha`, so it must be the base head AFTER the merge — the exact
    commit whose code a fresh interpreter would load."""
    h = build_merge(tmp_path, {"t1": {"autoloop/marker.py": "MARKER = 1\n"}})
    execution = h.push("t1")

    record = h.upgrades.load()
    assert record is not None, "a merge touching autoloop/ must offer a restart"
    assert record.status == UPGRADE_PENDING
    assert record.base_sha == h.head()
    assert record.candidate_sha == execution.candidate_sha
    assert record.task_id == "t1"
    assert record.paths == ["autoloop/marker.py"]
    assert record.repo_root == str(h.repo)
    assert [e["data"]["base_sha"] for e in entries(h.config, "self_upgrade_pending")] == [
        record.base_sha
    ]


def test_a_docs_only_merge_offers_no_restart(tmp_path):
    """The complement, and it is asserted against a merge that really
    happened: a test that only checked "no record" would pass just as well if
    the merge itself had been refused."""
    h = build_merge(tmp_path, {"t1": {"docs/NOTE.md": "hello\n"}})
    h.push("t1")

    assert entries(h.config, "auto_merge_pushed"), "the merge must have happened"
    assert h.upgrades.load() is None
    assert entries(h.config, "self_upgrade_pending") == []


def test_recording_the_signal_can_never_fail_the_merge(tmp_path, monkeypatch):
    """A raise from the recording site would reach `_guarded_attempt` and
    report a verified, pushed merge as `failed`. So a git that will not diff is
    logged and passed over — the loop keeps running the old code, which is
    exactly what it was doing anyway."""
    h = build_merge(tmp_path, {"t1": {"autoloop/marker.py": "MARKER = 1\n"}})

    def refuse(*_args, **_kwargs):
        raise RuntimeError("diff-tree exploded")

    # The MAIN checkout's gateway only — the same object `AutoMerger` is built
    # with. Patching the class would also break the WORKER gateway's
    # `commit_range_paths`, which the post-commit check and the review packet
    # both need, and the test would then be about a refused round instead.
    monkeypatch.setattr(h.orch._git, "changed_paths", refuse)
    h.push("t1")

    assert entries(h.config, "auto_merge_pushed"), "the merge still landed"
    assert h.upgrades.load() is None
    assert entries(h.config, "self_upgrade_error"), "and the failure is reported"


def test_loop_code_paths_matches_the_package_and_nothing_beside_it():
    """`autoloop/tests/...` counts: the claim is "any file under `autoloop/`",
    and narrowing it to non-test files would narrow the claim."""
    assert loop_code_paths(
        {
            "autoloop/policy.py",
            "autoloop/tests/test_policy.py",
            "autoloop/browser/session.py",
            "docs/AUTOLOOP.md",
            "autoloopish/thing.py",
            "lexy-app/backend/main.py",
        }
    ) == ["autoloop/browser/session.py", "autoloop/policy.py", "autoloop/tests/test_policy.py"]


# --- the boundary: where a replacement is allowed to happen ------------------


def orchestrator_at(tmp_path, phase, *, pending_request=None, upgrades=True):
    """The smallest orchestrator that can answer "is this a boundary" — no
    client, no executor, no git work. Every test below calls `run(max_steps=0)`,
    which returns before stepping, so none of those collaborators is reached.

    `upgrades` mirrors the production wiring: `cli._build_orchestrator` is the
    only construction that turns the boundary ON, so a caller that cannot act
    on it is never offered it."""
    config = make_config(tmp_path)
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.phase = phase.value
    state.outbox = "the next packet's payload"
    state.pending_request = pending_request
    store.save(state)
    registry = TaskRegistry([])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    def no_client():
        raise AssertionError("no client expected at a boundary check")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        self_upgrade_enabled=upgrades,
    )
    return orch, config


def test_a_pending_upgrade_ends_the_round_before_the_next_request_is_prepared(tmp_path):
    """`READY` with no packet prepared is the boundary: `_step_ready` is what
    builds a request, so nothing has been sent, nothing is awaited, and the
    payload is already durable in `state.outbox`."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())

    assert orch.run(max_steps=0) == SELF_UPGRADE
    assert entries(config, "self_upgrade_boundary")
    # The session is untouched — a replacement here loses nothing.
    assert orch.state.phase == Phase.READY.value
    assert orch.state.outbox == "the next packet's payload"


def test_no_upgrade_pending_is_an_ordinary_round(tmp_path):
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    assert orch.run(max_steps=0) == Phase.READY.value
    assert entries(config, "self_upgrade_boundary") == []


def test_a_packet_already_prepared_is_never_a_boundary(tmp_path):
    """`pending_request` outlives its own phase: a request answered and not yet
    consumed is still a packet this round owes something to."""
    request = PendingRequest(
        request_id="alr-x-0001",
        payload="p",
        prompt="prompt",
        prompt_sha256="0" * 64,
        head_sha="a" * 40,
        base_sha="a" * 40,
        report_sha256="1" * 64,
        timestamp="now",
    )
    orch, config = orchestrator_at(tmp_path, Phase.READY, pending_request=request)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())

    assert orch.run(max_steps=0) == Phase.READY.value
    assert entries(config, "self_upgrade_boundary") == []


@pytest.mark.parametrize(
    "phase",
    [Phase.DELIVERING, Phase.SUBMITTING, Phase.SUBMISSION_UNCONFIRMED, Phase.AWAITING,
     Phase.EXECUTING],
)
def test_no_phase_but_ready_is_a_boundary(tmp_path, phase):
    """Every one of these is mid-round by construction — a packet in flight, a
    reviewer holding one, or an agent writing into a worker repo."""
    orch, config = orchestrator_at(tmp_path, phase)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())

    assert orch.run(max_steps=0) == phase.value
    assert entries(config, "self_upgrade_boundary") == []


def test_a_parked_loop_reports_the_park_not_the_upgrade(tmp_path):
    """A terminal phase wins: a restart nobody can act on is not the answer to
    "why did the loop stop"."""
    orch, config = orchestrator_at(tmp_path, Phase.NEEDS_USER)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())

    assert orch.run(max_steps=0) == Phase.NEEDS_USER.value


@pytest.mark.parametrize(
    "status",
    [UPGRADE_EXECED, UPGRADE_PREFLIGHT_FAILED, UPGRADE_UNAPPLICABLE, UPGRADE_EXEC_FAILED],
)
def test_a_settled_record_is_never_offered_again(tmp_path, status):
    """The one-shot, seen from the boundary: only `pending` is offered, so
    `execed` — a replacement that is already running — cannot come back round.

    The other three are no longer written into `status` by any boundary (they
    are OUTCOME names now, and their records stay `pending` so the next process
    can retry), and they stay in this parametrisation for the state dir this
    build inherits: one written by a pre-2026-08-31 loop can hold any of them,
    and "not pending, so not offered" is the answer that keeps such a record
    from being acted on twice."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    UpgradeStore(config.pending_upgrade_file).save(pending_record(status=status))

    assert orch.run(max_steps=0) == Phase.READY.value


def test_an_unreadable_record_is_no_record(tmp_path):
    """Fail-closed here means "keep running the code that works"."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.pending_upgrade_file.write_text("{not json", encoding="utf-8")

    assert orch.run(max_steps=0) == Phase.READY.value


def test_a_record_with_no_base_sha_is_never_offered(tmp_path):
    """A boundary nobody could ever answer is not offered at all.

    Every bound in this design is keyed on `base_sha` — the per-process
    decline, `_run_continuous`'s `answered_upgrades`, the one-shot on `execed`
    — and no boundary outcome but the exec moves the record out of `pending`.
    So a record with no sha to key on would be offered, carried on from,
    offered again on the very next call, forever, at the speed of the loop.
    Nothing the merger writes lacks one; a record that does is hand-written or
    corrupt, and refusing it keeps this process running the code it has."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    UpgradeStore(config.pending_upgrade_file).save(pending_record(base_sha=""))

    assert orch.run(max_steps=0) == Phase.READY.value
    assert orch.run(max_steps=0) == Phase.READY.value, "and not on the next round"
    assert entries(config, "self_upgrade_boundary") == [], (
        "no boundary, so no missing outcome either — the silence this task is "
        "about is a boundary with nothing after it, and there is no boundary"
    )


#: What `pending_upgrade.json` can hand back in `base_sha` that is NOT a key a
#: bound can be hung on. `UpgradeStore.load` builds the record with
#: `PendingUpgrade(**data)` and coerces nothing, so every one of these is
#: reachable from a hand-edited, half-written or foreign state dir. They divide
#: into the two ways they used to kill the loop, and the suite covers both:
#: `None`/`5`/`{}` raise TypeError on the `[:12]` slices the exec path prints,
#: and `[]`/`{}` raise it on `set.add` and on the `in` test the decline makes.
UNUSABLE_SHAS = [None, 5, {"sha": "b" * 40}, ["b" * 40], ""]
UNUSABLE_IDS = ["none", "int", "dict", "list", "empty"]


@pytest.mark.parametrize("sha", UNUSABLE_SHAS, ids=UNUSABLE_IDS)
def test_only_a_non_empty_string_is_a_sha_a_bound_can_be_keyed_on(sha):
    """The one predicate, tested where it is cheapest — a pure function, no
    repository and no process. `upgrade_bound_sha` is what
    `_self_upgrade_due`, `_self_upgrade_at_boundary` and `_defer_self_upgrade`
    all ask, so a change to what counts as usable moves in one place."""
    assert upgrade_bound_sha(pending_record(base_sha=sha)) == ""
    assert upgrade_bound_sha(None) == "", "no record is no key either"
    assert upgrade_bound_sha(pending_record()) == "b" * 40, "and a real one is"


@pytest.mark.parametrize("sha", UNUSABLE_SHAS, ids=UNUSABLE_IDS)
def test_a_record_whose_base_sha_is_not_a_key_is_never_offered(tmp_path, sha):
    """`test_a_record_with_no_base_sha_is_never_offered` generalised past the
    empty string, because the empty string is the one shape that fails SAFELY.
    A `list` or a `dict` there reaches `base_sha in self._declined_upgrades` and
    raises `TypeError: unhashable type` — a boundary CHECK that kills the loop,
    which is the outage's own shape reached from a fresh direction."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    UpgradeStore(config.pending_upgrade_file).save(pending_record(base_sha=sha))

    assert orch.run(max_steps=0) == Phase.READY.value
    assert orch.run(max_steps=0) == Phase.READY.value, "and not on the next round"
    assert entries(config, "self_upgrade_boundary") == [], (
        "no boundary, so no missing outcome either"
    )


@pytest.mark.parametrize("sha", UNUSABLE_SHAS, ids=UNUSABLE_IDS)
def test_a_sha_that_is_not_a_string_is_refused_rather_than_declined(tmp_path, sha):
    """The same guard on the entry point a CALLER reaches with a value read off
    the same file. `not base_sha` alone lets `["b"*40]` through to the
    membership test, and the decline is called precisely to keep a process
    running past a boundary — it must not be what ends it."""
    orch, _config = orchestrator_at(tmp_path, Phase.READY)
    assert orch.decline_self_upgrade(sha) is False


def test_a_record_whose_paths_field_is_not_a_list_still_reaches_an_outcome(
    tmp_path, monkeypatch
):
    """The same silence, arrived at from the other direction.

    `paths` is evidence for a human and nothing branches on it, but it is read
    into the entry that ANNOUNCES the boundary — so a record carrying `null`
    there (a hand-edited file, a half-written one) used to raise a TypeError
    while that entry was being built: no entry, and the traceback takes the
    loop with it. `UpgradeStore.load` validates the record's SHAPE, never its
    field types, so this is reachable from any state dir a person has touched."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    UpgradeStore(config.pending_upgrade_file).save(pending_record(paths=None))

    assert orch.run(max_steps=0) == SELF_UPGRADE
    boundary = entries(config, "self_upgrade_boundary")
    assert boundary and boundary[0]["data"]["paths"] == []

    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    recording_execv(monkeypatch)

    with pytest.raises(Execed):
        cli._self_upgrade_at_boundary(config, held_lock(config))

    logged = entries(config, "self_upgrade_exec")
    assert logged and logged[0]["data"]["paths"] == [], (
        "and the handoff entry, written in the last instant before `execv`, "
        "builds too"
    )


def test_a_record_whose_repo_root_is_not_a_path_reaches_an_outcome_too(tmp_path):
    """Same family, one field over. `Path(None)` and `Path([...])` raise
    TypeError, and that read happens AFTER the caller has written
    `self_upgrade_boundary` — so the crash lands exactly in the gap the outage
    lived in. Anything unreadable as a path is simply not the tree this process
    imports from, which is an outcome, with an entry of its own."""
    config = make_config(tmp_path)
    UpgradeStore(config.pending_upgrade_file).save(
        pending_record(repo_root=["/one", "/other"])
    )

    assert cli._self_upgrade_at_boundary(config, None) == UPGRADE_UNAPPLICABLE
    assert entries(config, f"self_upgrade_{UPGRADE_UNAPPLICABLE}")
    assert UpgradeStore(config.pending_upgrade_file).load().status == UPGRADE_PENDING


def test_a_repo_root_the_filesystem_refuses_reaches_an_outcome_as_well(tmp_path):
    """One step further along the same line. `str()` makes `Path(...)` safe, but
    `resolve()` goes to the filesystem: a NUL inside the string — `chr(0)` here,
    and a legal JSON escape in the file — raises `ValueError: embedded null
    byte` on the `os.lstat` underneath it, in the same gap and with the same
    ending. A repo_root this process cannot even resolve is certainly not the
    tree it imports from, so it is the `unapplicable` outcome it already had a
    name for, reported with the error rather than raised."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    root = f"{tmp_path}{chr(0)}/merged"
    store.save(pending_record(repo_root=root))
    try:
        Path(root).resolve()
    except (OSError, ValueError):
        # Which interpreter is running decides whether `realpath` re-raises a
        # null byte or swallows it, so the STRONGER assertion is made only
        # where the raise is real. The weaker one — an outcome rather than a
        # traceback — is the claim, and it holds either way.
        refused = True
    else:
        refused = False

    assert cli._self_upgrade_at_boundary(config, None) == UPGRADE_UNAPPLICABLE

    logged = entries(config, f"self_upgrade_{UPGRADE_UNAPPLICABLE}")
    assert logged, "the boundary is answered, never left hanging"
    if refused:
        assert "cannot resolve" in logged[0]["data"]["detail"], (
            "and it says which field it could not use, with the error"
        )
    assert store.load().status == UPGRADE_PENDING, "still retryable, as every refusal is"


def test_an_orchestrator_that_cannot_act_on_the_boundary_is_not_offered_it(tmp_path):
    """The record lives under `state_dir`, so every orchestrator sharing that
    directory can see it — tests, embedders, and (until brw-16, 2026-08-25,
    retired the command) `smoke-browser`'s own, which started at `ready` with
    no pending request: the boundary shape exactly. Offered the boundary, an
    unrelated pending upgrade would end a round that had nothing to do with it.
    `cli._build_orchestrator` is the one construction that opts in."""
    orch, config = orchestrator_at(tmp_path, Phase.READY, upgrades=False)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())

    assert orch.run(max_steps=0) == Phase.READY.value
    assert entries(config, "self_upgrade_boundary") == []


# --- the replacement itself --------------------------------------------------


def held_lock(config) -> LoopLock:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return LoopLock(config.state_dir).acquire()


def test_the_boundary_replaces_the_process_with_a_fresh_interpreter(tmp_path, monkeypatch):
    """The whole feature. `os.execv` — not `importlib.reload`: reloading modules
    in a running orchestrator leaves half-reloaded modules and live objects
    holding the old classes, in a process that authorizes git pushes.

    The successor's command line is DERIVED, not replayed, and this drives it
    from the worst case: the operator typed `start`, so `sys.argv[1:]` is
    `["start"]` — a verb whose successor refuses on its own live lock (see
    `test_a_start_shaped_successor_refuses_on_the_lock_the_exec_preserved`).
    """
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    lock = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    monkeypatch.setattr(sys, "argv", ["/x/autoloop/__main__.py", "start"])
    calls = recording_execv(monkeypatch)
    args = argparse.Namespace(config=None, continuous=True, null_executor=False)

    with pytest.raises(Execed):
        cli._self_upgrade_at_boundary(config, lock, args)

    assert calls == [(sys.executable, [sys.executable, "-m", "autoloop", "run", "--continuous"])], (
        "the documented launch shape, rebuilt — argv[0] under `-m` is the path "
        "to __main__.py, and re-running THAT as a script breaks its imports; "
        "the verb is the one that CONTINUES a loop, not the one that was typed"
    )
    logged = entries(config, "self_upgrade_exec")
    assert logged and logged[0]["data"]["argv"] == calls[0][1], (
        "and the transcript records the command line actually handed over"
    )
    assert store.load().status == UPGRADE_EXECED, "the one-shot, recorded BEFORE the exec"


def test_the_handoff_token_is_armed_in_the_environment_at_the_moment_of_the_exec(
    tmp_path, monkeypatch
):
    """The successor is authorized by something it INHERITS, so the token has
    to be set in this process's environment at the instant `os.execv` is
    called — that call is the only thing that carries it across. Observed at
    the call itself rather than before it, because "armed at some point" and
    "armed when the image is replaced" are different claims."""
    config = make_config(tmp_path)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())
    lock = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    at_exec: list = []

    def record(path, argv):
        at_exec.append((inherited_token(), lock.read().exec_handoff))
        raise Execed(argv)

    monkeypatch.setattr(os, "execv", record)

    with pytest.raises(Execed):
        cli._self_upgrade_at_boundary(config, lock)

    assert len(at_exec) == 1
    token, marker = at_exec[0]
    assert token and marker and marker["token"] == token, (
        "the lock file and the environment carry the same one-use token"
    )
    assert marker["pid"] == os.getpid() and marker["run_id"] == lock.run_id


def test_the_preflight_runs_before_anything_is_replaced(tmp_path, monkeypatch):
    """Order matters: a tree that does not import must be found out while this
    process is still the one running."""
    config = make_config(tmp_path)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())
    lock = held_lock(config)
    seen: list = []

    def preflight(root):
        seen.append(("preflight", root))
        return True, ""

    def record(path, argv):
        seen.append(("exec", path))
        raise Execed(argv)

    monkeypatch.setattr(cli, "_preflight_import", preflight)
    monkeypatch.setattr(os, "execv", record)

    with pytest.raises(Execed):
        cli._self_upgrade_at_boundary(config, lock)

    assert [kind for kind, _ in seen] == ["preflight", "exec"]
    assert seen[0][1] == cli._package_root(), "the tree the replacement will load"


def test_a_tree_that_does_not_import_is_not_exec_ed_and_the_loop_carries_on(
    tmp_path, monkeypatch
):
    """A bad merge must be REPORTED, not fatal. `no_process_replacement` is what
    makes "not exec'ed" a real assertion here: an exec would raise."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    lock = held_lock(config)
    monkeypatch.setattr(
        cli, "_preflight_import", lambda root: (False, "SyntaxError: invalid syntax")
    )

    assert cli._self_upgrade_at_boundary(config, lock) == UPGRADE_PREFLIGHT_FAILED

    record = store.load()
    assert record.status == UPGRADE_PENDING, (
        "REPORTED and REFUSED, not settled: the refusal is this checkout's "
        "answer for as long as the tree stays broken, and the merge that fixes "
        "it lands in the same tree — so the record has to still be there"
    )
    assert record.settled_at == "", "nothing was settled, so nothing settled it"
    assert record.detail.startswith(UPGRADE_PREFLIGHT_FAILED)
    assert "SyntaxError" in record.detail
    logged = entries(config, f"self_upgrade_{UPGRADE_PREFLIGHT_FAILED}")
    assert logged and "SyntaxError" in logged[0]["data"]["detail"]
    # And the lock was never armed: nothing was going to be handed anywhere.
    assert lock.read().exec_handoff is None
    # RETRYABLE IS NOT WEAKER. The record staying pending buys a later process
    # another attempt, not a softer answer: the same tree gets the same refusal,
    # and `no_process_replacement` is what makes "no exec" an assertion — an
    # exec from here would raise instead of passing quietly.
    assert cli._self_upgrade_at_boundary(config, lock) == UPGRADE_PREFLIGHT_FAILED
    assert lock.read().exec_handoff is None


def test_a_failed_preflight_is_not_retried_every_round(tmp_path, monkeypatch):
    """The bound on the retry, at the level where it now lives, driven in the
    order production drives it: boundary, refusal, decline, next round.

    The record stays `pending` on purpose — a later process must be able to try
    the same merge — and `pending` is exactly what `_self_upgrade_due` offers,
    so without a per-process bound `Orchestrator.run` would hand the process
    back at every single round and launch a preflight interpreter each time: a
    loop that reports busily and never advances. The bound is the DECLINE,
    which `_run_continuous` re-applies to the orchestrator it rebuilds each
    iteration (`answered_upgrades`), so this asserts it on the real class
    rather than on a status written to disk."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    lock = held_lock(config)
    attempts: list = []

    def preflight(root):
        attempts.append(root)
        return False, "boom"

    monkeypatch.setattr(cli, "_preflight_import", preflight)

    assert orch.run(max_steps=0) == SELF_UPGRADE, "offered once"
    assert cli._self_upgrade_at_boundary(config, lock) == UPGRADE_PREFLIGHT_FAILED
    assert orch.decline_self_upgrade("b" * 40) is True

    assert orch.run(max_steps=0) == Phase.READY.value, "and not again in this process"
    assert orch.run(max_steps=0) == Phase.READY.value
    assert len(attempts) == 1, "one preflight per process, not one per round"
    assert len(entries(config, "self_upgrade_boundary")) == 1
    assert store.load().status == UPGRADE_PENDING, "still there for the next one"


@pytest.mark.parametrize(
    "outcome",
    [UPGRADE_PREFLIGHT_FAILED, UPGRADE_UNAPPLICABLE, UPGRADE_EXEC_FAILED],
)
def test_a_refused_handoff_is_still_there_for_the_next_process(
    tmp_path, monkeypatch, outcome
):
    """08:15:30, generalised to every non-exec outcome. One process reaches the
    boundary and cannot hand off; the NEXT one exec's the same record — which is
    exactly what happened on 2026-08-27 and is the reason none of these is a
    judgement the later process has to inherit.

    Each parametrisation drives the real refusal, not a hand-written status:
    a preflight that says no, a record naming another checkout, and a handoff
    with no lock to arm."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(
        pending_record(repo_root=str(tmp_path / "elsewhere"))
        if outcome == UPGRADE_UNAPPLICABLE
        else pending_record()
    )
    lock = held_lock(config)
    monkeypatch.setattr(
        cli,
        "_preflight_import",
        (lambda root: (False, "SyntaxError: invalid syntax"))
        if outcome == UPGRADE_PREFLIGHT_FAILED
        else (lambda root: (True, "")),
    )

    # The process that cannot: `None` for the lock is the unarmable handoff.
    refused = cli._self_upgrade_at_boundary(
        config, None if outcome == UPGRADE_EXEC_FAILED else lock
    )
    assert refused == outcome
    assert entries(config, f"self_upgrade_{outcome}"), "and it said so"
    assert store.load().status == UPGRADE_PENDING, "the upgrade is still to do"

    # ---- a later process: a lock it can arm, a tree that imports ------------
    if outcome == UPGRADE_UNAPPLICABLE:
        # The one outcome that is a fact about the RECORD rather than about the
        # process, so the later process is one whose checkout the merge moved.
        store.save(pending_record())
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    recording_execv(monkeypatch)

    with pytest.raises(Execed):
        cli._self_upgrade_at_boundary(config, lock)

    assert entries(config, "self_upgrade_exec"), "the second process handed off"


def test_a_merge_in_another_checkout_is_not_a_reason_to_restart(tmp_path, monkeypatch):
    """If the merged tree is not the one this process imported from, replacing
    it would load the same code again."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record(repo_root=str(tmp_path / "somewhere-else")))
    lock = held_lock(config)
    monkeypatch.setattr(
        cli, "_preflight_import", lambda root: pytest.fail("must not preflight")
    )

    assert cli._self_upgrade_at_boundary(config, lock) == UPGRADE_UNAPPLICABLE
    assert store.load().status == UPGRADE_PENDING, (
        "and it is a fact about THIS process's checkout, not about the merge — "
        "the process that does run that tree still has the upgrade to do"
    )
    logged = entries(config, f"self_upgrade_{UPGRADE_UNAPPLICABLE}")
    assert logged and "somewhere-else" in logged[0]["data"]["detail"], (
        "an outcome no event mirrors is invisible to `status`, to the "
        "dashboard and to the log after the loop is gone"
    )
    assert logged[0]["data"]["candidate_sha"] == "c" * 40, (
        "the entry names the merge it is about, both shas — the 08:13:47 "
        "reconstruction had neither"
    )


def test_the_replacement_is_refused_when_the_lock_cannot_be_armed(tmp_path, monkeypatch):
    """Its successor would find a live lock — its own pid — fail closed and end
    the run. Refusing costs one delayed upgrade; the alternative costs the run."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))

    assert cli._self_upgrade_at_boundary(config, None) == UPGRADE_EXEC_FAILED
    restored = store.load()
    assert restored.status == UPGRADE_PENDING, (
        "the one-shot marker was already written — `execed` has to be durable "
        "BEFORE the exec — so a refusal after it has to put the record back, "
        "or the upgrade is lost to a process that never went anywhere"
    )
    assert restored.settled_at == ""
    logged = entries(config, f"self_upgrade_{UPGRADE_EXEC_FAILED}")
    assert logged and "lock could not be armed" in logged[0]["data"]["detail"], (
        "the refusal names itself in the transcript — this is the branch whose "
        "own message the 2026-08-27 postmortem suspected, and it would have "
        "left an entry if it had fired"
    )


def test_an_exec_that_is_refused_disarms_the_lock_again(tmp_path, monkeypatch):
    """`execv` raising means the process is still here, still holding the lock,
    and no successor is coming — so the marker has to go, or the NEXT acquire
    in this pid would adopt a handoff that never happened, and the RECORD has
    to stop saying `execed`, or the confirmation one iteration later reports a
    replacement that did not happen (the loop-level half is
    `test_a_refused_exec_is_never_confirmed_as_a_replacement_that_happened`)."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    lock = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))

    def refuse(path, argv):
        raise OSError("Exec format error")

    monkeypatch.setattr(os, "execv", refuse)

    assert cli._self_upgrade_at_boundary(config, lock) == UPGRADE_EXEC_FAILED
    assert lock.read().exec_handoff is None
    assert inherited_token() is None, (
        "the token goes with the marker — a successor that never started "
        "cannot need it, and the subprocesses this run keeps spawning would "
        "otherwise inherit it"
    )
    restored = store.load()
    assert restored.status == UPGRADE_PENDING, (
        "the record is put BACK, not left saying `execed`: `execed` means a "
        "successor is running, and `_confirm_self_upgrade` retires it one "
        "iteration later — but this process never went anywhere, and the "
        "merge it could not exec into is still waiting for a process that can"
    )
    assert restored.settled_at == ""
    assert "Exec format error" in restored.detail
    assert cli._self_upgrade_at_boundary(config, lock) == UPGRADE_EXEC_FAILED, (
        "a second attempt in this process reaches the same refusal and says so "
        "again — what stops that being a spin is the per-process decline, not "
        "a status on disk (`test_a_failed_preflight_is_not_retried_every_round`)"
    )


def test_a_record_that_cannot_be_put_back_is_removed_rather_than_left_execed(
    tmp_path, monkeypatch
):
    """The fail-open the retryable record introduces, closed.

    `execed` is written before the exec because it has to survive it, so a
    refusal afterwards depends on a SECOND write to put the record back. If
    that write fails and the record is simply left alone, the state dir says
    `execed` — and `_confirm_self_upgrade` at the top of the next iteration
    retires it and logs a replacement that never happened, with the pending
    upgrade destroyed on the way past. So the fallback is to REMOVE it: that
    costs a delayed restart (the merged code is on disk, and the next process
    START loads it) instead of a false confirmation plus a lost upgrade."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    saves: list = []
    real_save = UpgradeStore.save

    def flaky_save(self, record):
        saves.append(record.status)
        if len(saves) == 1:
            return real_save(self, record)  # the one-shot marker lands
        raise OSError("state dir went read-only")

    monkeypatch.setattr(UpgradeStore, "save", flaky_save)

    # No lock to arm: the refusal that happens AFTER the marker was written.
    assert cli._self_upgrade_at_boundary(config, None) == UPGRADE_EXEC_FAILED

    assert saves == [UPGRADE_EXECED, UPGRADE_PENDING], "it tried to put it back"
    assert store.load() is None, "and removed it when it could not"
    assert not config.pending_upgrade_file.exists()
    logged = entries(config, f"self_upgrade_{UPGRADE_EXEC_FAILED}")
    assert logged and "REMOVED" in logged[0]["data"]["detail"], (
        "an operator has to be told the marker is gone — this is the one "
        "outcome that needs a restart rather than a boundary"
    )
    monkeypatch.undo()
    assert cli._confirm_self_upgrade(config) is False
    assert entries(config, "self_upgrade_confirmed") == [], (
        "nothing left saying `execed`, so nothing to confirm falsely"
    )


def test_a_marker_that_cannot_be_written_leaves_the_pending_record_alone(
    tmp_path, monkeypatch
):
    """The other side of that fallback, and the reason it is not unconditional.

    Here the write that fails IS the one-shot marker — so the exec never
    happened, and `UpgradeStore.save` being temp + `os.replace` means the file
    still holds the `pending` record it held a moment ago. Clearing it would
    answer a write that never landed by destroying a healthy record, and the
    upgrade would be lost to a transient ENOSPC."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    lock = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))

    def refuse_save(self, record):
        raise OSError("No space left on device")

    monkeypatch.setattr(UpgradeStore, "save", refuse_save)

    assert cli._self_upgrade_at_boundary(config, lock) == UPGRADE_EXEC_FAILED

    monkeypatch.undo()
    assert store.load().status == UPGRADE_PENDING, "untouched, and still to do"
    assert lock.read().exec_handoff is None, "nothing was armed, either"
    logged = entries(config, f"self_upgrade_{UPGRADE_EXEC_FAILED}")
    assert logged and "No space left on device" in logged[0]["data"]["detail"]
    assert "retryable" in logged[0]["data"]["detail"]


# --- no boundary ends in silence, and none of them ends the process ----------
#
# 2026-08-27, verbatim from the deployment transcript:
#
#   08:13:47  self_upgrade_boundary  split-05   <- reached
#             ...nothing. The process was gone.
#   08:15:26  an operator ran `python3 -m autoloop start` by hand
#   08:15:30  self_upgrade_boundary  split-05   <- the SAME pending record
#   08:15:30  self_upgrade_exec      split-05
#
# The divergence is the code PATH, not the verb: `_run_continuous` handed the
# boundary to `_self_upgrade_at_boundary`, and the single-round `_run_locked`
# — every non-`--continuous` entry, `run`, `--retry`, `--answer`, `--resubmit`
# and `resume` alike — answered `SELF_UPGRADE` by returning. No store write, no
# transcript call, mid-session, with the record left `pending`.
#
# That last fact is what excluded every other candidate at the time, and the
# argument is worth keeping because it is structural rather than circumstantial:
# the build running that day SETTLED each of the other outcomes — wrote
# `preflight_failed` / `unapplicable` / `exec_failed` into `status` — and
# `_self_upgrade_due` only ever offers `pending`. So none of them could have
# ended in "no event + still pending + exec'd two minutes later"; the record
# would not have been offered again at 08:15:30. (Since 2026-08-31 those three
# leave the record `pending` too, which is why each of them now has a test
# proving the next process still performs the upgrade — the property this
# transcript demonstrated, made general.)


def test_a_boundary_with_nothing_left_to_act_on_still_says_so(tmp_path):
    """The fallthrough with no record to settle, and therefore the one that had
    no event at all. The caller has already written `self_upgrade_boundary`, so
    a quiet return leaves a boundary with nothing after it — the exact shape
    that made the outage unreadable."""
    config = make_config(tmp_path)
    UpgradeStore(config.pending_upgrade_file).save(pending_record(status=UPGRADE_EXECED))

    assert cli._self_upgrade_at_boundary(config, None) == "none"

    logged = entries(config, f"self_upgrade_{UPGRADE_NONE}")
    assert logged and logged[0]["data"]["status"] == UPGRADE_EXECED
    assert "carries on" in logged[0]["data"]["detail"]


def test_a_boundary_with_no_record_at_all_says_so_too(tmp_path):
    """Same branch, the other half of its condition — a record removed under a
    running loop (an operator, a wiped state dir) must not be a silent return
    either."""
    config = make_config(tmp_path)

    assert cli._self_upgrade_at_boundary(config, None) == "none"
    assert entries(config, f"self_upgrade_{UPGRADE_NONE}")


@pytest.mark.parametrize("sha", UNUSABLE_SHAS, ids=UNUSABLE_IDS)
def test_a_record_that_goes_unusable_between_the_two_reads_reaches_an_outcome(
    tmp_path, monkeypatch, sha
):
    """THE SECOND READ. The orchestrator reads the record to decide there is a
    boundary; `_self_upgrade_at_boundary` reads the same file again and acts on
    what IT got back. Between the two the file can be rewritten, hand-edited or
    truncated — and a `base_sha` that is not a string then reaches
    `mark_exec_handoff(f"self_upgrade {base_sha[:12]}")`, which raises TypeError
    on a dict, an int or null: after `self_upgrade_boundary`, before any
    outcome. That is 2026-08-27's silence, reached without any of that day's
    causes.

    The preflight is stubbed PASSING and a real lock is held on purpose: both
    would otherwise be the thing that stopped the exec, and this test is about
    the guard. `no_process_replacement` is the backstop — reaching the real
    `os.execv` fails the test loudly.
    """
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record(base_sha=sha))
    on_disk = config.pending_upgrade_file.read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))

    assert cli._self_upgrade_at_boundary(config, held_lock(config)) == UPGRADE_NONE

    logged = entries(config, f"self_upgrade_{UPGRADE_NONE}")
    assert len(logged) == 1, "one boundary, one outcome, never silence"
    assert "carries on" in logged[0]["data"]["detail"]
    assert type(sha).__name__ in logged[0]["data"]["detail"] or sha == "", (
        "and it names WHAT it could not use, which is what an operator has to "
        "fix by hand"
    )
    assert config.pending_upgrade_file.read_text(encoding="utf-8") == on_disk, (
        "nothing was written: a record this process could not make sense of is "
        "the evidence for fixing it, and `_carry_on_upgrade` would rewrite it"
    )


@pytest.mark.parametrize("sha", UNUSABLE_SHAS, ids=UNUSABLE_IDS)
def test_a_record_that_goes_unusable_at_the_boundary_does_not_end_continuous_mode(
    tmp_path, monkeypatch, sha
):
    """The same race, driven through the loop it would kill.

    `_run_continuous` re-reads the record beside the decision so it can bound
    the carry-on (`answered_upgrades`), and `set.add` on a `list` or a `dict`
    raises `TypeError: unhashable type` — one statement after the boundary
    carried on, with the traceback taking continuous mode down. Every non-exec
    outcome must leave this loop RUNNING, so the proof is that iteration two
    happened: `rounds` has two entries, and the second was recorded at the top
    of an iteration that could only exist if the first one returned.

    The orchestrator is a stand-in BECAUSE the real one refuses this record —
    that refusal is the outer guard and is tested above. This test is about
    what the cli does when the boundary arrives anyway, which is the case a
    file rewritten between the two reads produces.
    """
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record(base_sha=sha))
    on_disk = config.pending_upgrade_file.read_text(encoding="utf-8")
    ready_session(config)
    lock = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    rounds: list = []
    built: list = []

    class BoundaryThenStop:
        """Offers the boundary in round one and ends the test from inside
        iteration two — so reaching it at all is the assertion."""

        def __init__(self, state):
            self.state = state
            self.declined: list = []

        def run(self, *_args, **_kwargs):
            rounds.append(len(rounds))
            if len(rounds) >= 2:
                raise StopLoop()
            return SELF_UPGRADE

        def decline_self_upgrade(self, base_sha):
            self.declined.append(base_sha)
            return True

    def build(config_, args_, store_, state_, task_store_, registry_):
        built.append(BoundaryThenStop(state_))
        return built[-1]

    monkeypatch.setattr(cli, "_build_orchestrator", build)
    args = argparse.Namespace(config=None, continuous=True, null_executor=True)

    with pytest.raises(StopLoop):
        cli._run_continuous(args, config, lock)

    assert len(rounds) == 2, (
        f"base_sha={sha!r} ended the loop — the process must still be here"
    )
    assert entries(config, f"self_upgrade_{UPGRADE_NONE}"), (
        "and the boundary is answered in the transcript, not left hanging"
    )
    assert built[1].declined == [], (
        "the round-two orchestrator is handed no sha to re-decline, because "
        "there was none to key on. That a REAL orchestrator then offers no "
        "boundary for such a record — the other half of the no-spin claim — is "
        "`test_a_record_whose_base_sha_is_not_a_key_is_never_offered`, not this "
        "stand-in, whose round two ends the test unconditionally"
    )
    assert config.pending_upgrade_file.read_text(encoding="utf-8") == on_disk, (
        "and the malformed record is left for a person to look at"
    )


def test_a_single_round_boundary_survives_a_record_it_cannot_key_on(
    tmp_path, capsys
):
    """`_defer_self_upgrade`, same defect, other path. It hands its sha to
    `decline_self_upgrade`, where `["b"*40]` raises on the membership test —
    ending the single-round `run` at exactly the boundary this function exists
    to carry the process past. The stand-in key bounds it just as the vanished
    record does: carried on from once, stopped the second time."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    UpgradeStore(config.pending_upgrade_file).save(pending_record(base_sha=["b" * 40]))

    assert cli._defer_self_upgrade(config, orch) is True, "carry on"
    assert cli._defer_self_upgrade(config, orch) is False, "but not forever"

    logged = entries(config, f"self_upgrade_{UPGRADE_DEFERRED}")
    assert len(logged) == 2, "both boundaries answered in the transcript"
    assert logged[0]["data"]["base_sha"] == "", (
        "the entry reports no key rather than the value that is not one — and "
        "builds at all, which a non-serialisable field would not"
    )
    assert UpgradeStore(config.pending_upgrade_file).load().status == UPGRADE_PENDING
    capsys.readouterr()


def single_round_args(max_steps=0):
    """A single-round `run`: no `--continuous`, no manual flag. `max_steps=0`
    keeps `Orchestrator.run` above its first step — the boundary check runs
    before the budget check, so a real orchestrator answers the real question
    without needing a client, an executor or a git checkout."""
    return argparse.Namespace(
        config=None,
        continuous=False,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=max_steps,
        null_executor=True,
    )


def real_orchestrator(config_, args_, store_, state_, task_store_, registry_):
    """Stand in for `cli._build_orchestrator` with the real class, built on the
    objects `_run_locked` hands it — so the boundary, the decline and the step
    budget under test are the production ones. Only the collaborators a
    boundary check never reaches are left out (no client, no executor, no git);
    `orchestrator_at` above leaves out the same ones for the same reason."""

    def no_client():
        raise AssertionError("no client expected at a boundary check")

    return Orchestrator(
        config=config_,
        store=store_,
        state=state_,
        policy=PolicyEngine(config_.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config_.transcript_file),
        client_factory=no_client,
        registry=registry_,
        task_store=task_store_,
        manifest_store=ManifestStore(config_.manifests_dir),
        self_upgrade_enabled=True,
    )


def ready_session(config) -> LoopState:
    state = LoopState.new(URL)
    state.phase = Phase.READY.value
    state.outbox = "the next packet's payload"
    StateStore(config.state_file).save(state)
    TaskStore(config.tasks_file).save(TaskRegistry([]))
    return state


def test_the_single_round_path_records_the_boundary_and_keeps_running(
    tmp_path, monkeypatch, capsys
):
    """THE OUTAGE, end to end, through the real `_run_locked` with a real
    orchestrator. Three claims, and the third is the one that took the loop
    down: the outcome is named in the transcript, the process does NOT end at
    the boundary, and the record is left `pending` so the next process can
    still perform the upgrade."""
    config = make_config(tmp_path)
    upgrades = UpgradeStore(config.pending_upgrade_file)
    upgrades.save(pending_record())
    ready_session(config)
    monkeypatch.setattr(cli, "_build_orchestrator", real_orchestrator)

    rc = cli._run_locked(single_round_args(), config)

    assert rc == 0
    boundaries = entries(config, "self_upgrade_boundary")
    deferred = entries(config, f"self_upgrade_{UPGRADE_DEFERRED}")
    assert len(boundaries) == 1, "the boundary was reached once"
    assert len(deferred) == 1, "and answered once — no boundary without an outcome"
    assert deferred[0]["data"]["base_sha"] == "b" * 40
    assert deferred[0]["data"]["task_id"] == "t1"
    assert "StateError" in deferred[0]["data"]["detail"], "and it says WHY, actionably"
    assert upgrades.load().status == UPGRADE_PENDING, (
        "nothing here judged the merged tree — nothing was even attempted — so "
        "the next `run --continuous` must still find it"
    )
    assert "carrying on" in capsys.readouterr().out


def test_a_deferred_boundary_leaves_the_upgrade_for_the_next_process(
    tmp_path, monkeypatch
):
    """08:15:30, reproduced: the record the single-round path declined is the
    same one a continuous process then execs. This is what makes "not settled"
    a real property rather than a comment."""
    config = make_config(tmp_path)
    upgrades = UpgradeStore(config.pending_upgrade_file)
    upgrades.save(pending_record())
    ready_session(config)
    monkeypatch.setattr(cli, "_build_orchestrator", real_orchestrator)

    cli._run_locked(single_round_args(), config)

    # ---- the operator starts a continuous loop; same state dir, same record --
    lock = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    recording_execv(monkeypatch)

    with pytest.raises(Execed):
        cli._self_upgrade_at_boundary(config, lock)

    assert entries(config, "self_upgrade_exec"), "the upgrade was still there to do"


def test_a_declined_boundary_is_not_offered_twice_in_one_process(tmp_path):
    """The bound on carrying on. The record stays `pending` — which is what
    `_self_upgrade_due` offers — so without the decline the very next `run()`
    would return `SELF_UPGRADE` again, and the one after that, forever.

    Asserted on the ORCHESTRATOR rather than through the cli, because the
    decline is per instance: a future `_run_locked` that rebuilt one per step
    would lose the bound, and this is the test that fails when it does."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())

    assert orch.run(max_steps=0) == SELF_UPGRADE
    assert orch.decline_self_upgrade("b" * 40) is True

    assert orch.run(max_steps=0) == Phase.READY.value, "no second offer"
    assert orch.run(max_steps=0) == Phase.READY.value, "and no third"
    assert len(entries(config, "self_upgrade_boundary")) == 1, (
        "the entry is written after the decline check: a boundary already "
        "answered is not a new boundary, and logging it every round would bury "
        "the outcome that answered it"
    )
    assert orch.decline_self_upgrade("b" * 40) is False, (
        "the caller's loop bound — a second decline of the same sha means "
        "nothing moved, and `_run_locked` stops rather than re-entering"
    )
    assert UpgradeStore(config.pending_upgrade_file).load().status == UPGRADE_PENDING


def test_a_declined_boundary_is_only_declined_for_its_own_sha(tmp_path):
    """A later merge in the same session is a DIFFERENT upgrade, not a retry of
    the one this process turned down, and it gets its own boundary."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())

    assert orch.run(max_steps=0) == SELF_UPGRADE
    orch.decline_self_upgrade("b" * 40)
    store.save(pending_record(base_sha="d" * 40, task_id="t2"))

    assert orch.run(max_steps=0) == SELF_UPGRADE
    assert [e["data"]["base_sha"] for e in entries(config, "self_upgrade_boundary")] == [
        "b" * 40,
        "d" * 40,
    ]


def test_an_empty_sha_is_never_declined(tmp_path):
    """An empty sha is not a key, so it declines nothing: a set holding `""`
    would match no record and bound nothing. The caller substitutes
    `cli.MISSING_UPGRADE_RECORD` for exactly that reason (the test below)."""
    orch, _config = orchestrator_at(tmp_path, Phase.READY)
    assert orch.decline_self_upgrade("") is False


def test_a_boundary_whose_record_vanished_still_carries_on_and_is_still_bounded(
    tmp_path, capsys
):
    """The narrow race the decline has to survive: the record is gone by the
    time the outcome is decided. There is no sha to key on, so
    `MISSING_UPGRADE_RECORD` stands in — the loop carries on (an exit here
    would be the very failure this task is about), and a second disappearance
    in the same run stops instead of spinning on a flapping state dir."""
    orch, config = orchestrator_at(tmp_path, Phase.READY)

    assert cli._defer_self_upgrade(config, orch) is True, "carry on"
    assert cli._defer_self_upgrade(config, orch) is False, "but not forever"

    logged = entries(config, f"self_upgrade_{UPGRADE_DEFERRED}")
    assert len(logged) == 2, "and both are in the transcript, sha or no sha"
    assert logged[0]["data"]["base_sha"] == ""
    capsys.readouterr()


class BudgetSpy:
    """Records the step budget each re-entry is given, and spends two steps
    before the boundary — the shape `--max-steps` has to survive."""

    def __init__(self, state):
        self.state = state
        self.budgets: list = []
        self.declined: list = []
        self.steps_taken = 0

    def run(self, max_steps=None):
        self.budgets.append(max_steps)
        if len(self.budgets) == 1:
            self.steps_taken = 2
            return SELF_UPGRADE
        return Phase.READY.value

    def decline_self_upgrade(self, base_sha):
        self.declined.append(base_sha)
        return True


def test_a_declined_boundary_does_not_hand_out_a_second_step_budget(
    tmp_path, monkeypatch
):
    """`--max-steps` is a budget for the RUN. Re-entering `run()` with the same
    number would silently double it."""
    config = make_config(tmp_path)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())
    state = ready_session(config)
    spy = BudgetSpy(state)
    monkeypatch.setattr(cli, "_build_orchestrator", lambda *a, **k: spy)

    cli._run_locked(single_round_args(max_steps=5), config)

    assert spy.budgets == [5, 3], "two steps were spent before the boundary"
    assert spy.declined == ["b" * 40]


def test_the_step_budget_arithmetic_never_goes_negative():
    """`run(max_steps=0)` returns at the top of its loop without stepping, which
    is the right answer to a budget already spent; a negative would read as
    "no budget given" one call later."""
    assert cli._remaining_steps(None, 7) is None
    assert cli._remaining_steps(5, 2) == 3
    assert cli._remaining_steps(2, 5) == 0


# --- the successor's command line --------------------------------------------


def test_the_successor_command_continues_the_loop_whatever_verb_was_typed():
    """`sys.argv[1:]` is the verb an operator typed, and only one of them is a
    verb a successor can continue on. `start` re-runs `_cmd_start`, which reads
    the lock before acquiring it and refuses on the live one the exec preserved
    (the test below); `resume` and a bare `run` are single-round paths that
    would defer the upgrade they were exec'd to perform."""
    argv = cli._successor_argv(None)

    assert argv == [sys.executable, "-m", "autoloop", "run", "--continuous"]
    assert "start" not in argv and "resume" not in argv


def test_the_successor_keeps_the_config_and_the_dry_run_it_was_given():
    """The two flags whose loss changes what the successor DOES: a dropped
    `--config` lets its own `DEFAULT_CONFIG` repoint it at another deployment,
    and a dropped `--null-executor` turns a dry run into one that writes."""
    args = argparse.Namespace(config=Path("/tmp/elsewhere.toml"), null_executor=True)

    assert cli._successor_argv(args) == [
        sys.executable,
        "-m",
        "autoloop",
        "run",
        "--continuous",
        "--config",
        "/tmp/elsewhere.toml",
        "--null-executor",
    ]


def test_a_start_shaped_successor_refuses_on_the_lock_the_exec_preserved(
    tmp_path, monkeypatch, capsys
):
    """WHY the verb is not replayed, proved rather than asserted. `os.execv`
    preserves the pid and never releases the lock — that is the whole point —
    so a successor running `start` finds a LIVE lock naming itself, prints
    "already running" and returns 0. A successful exec would still have ended
    the loop, and with nothing in the transcript."""
    config = make_config(tmp_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    write_lock(
        config.state_dir,
        LockInfo(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=utcnow_iso(),
            run_id="the-run-that-exec-ed",
            state_dir=str(config.state_dir),
        ),
        handoff={"pid": os.getpid(), "run_id": "the-run-that-exec-ed", "token": "t"},
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        cli, "_cmd_run", lambda args: pytest.fail("start never got as far as running")
    )

    rc = cli._cmd_start(argparse.Namespace(config=None, check_only=False))

    assert rc == 0
    assert "already running — nothing to do" in capsys.readouterr().out
    assert entries(config) == [], "and not one word of it in the transcript"


# --- the preflight actually launches an interpreter --------------------------


def test_the_preflight_imports_the_tree_it_is_pointed_at(tmp_path):
    """Both directions, with a real subprocess. A preflight that never launched
    an interpreter would pass for a tree with a syntax error in it."""
    ok, detail = cli._preflight_import(REPO_ROOT)
    assert ok, detail

    broken = tmp_path / "checkout"
    (broken / "autoloop").mkdir(parents=True)
    (broken / "autoloop" / "__init__.py").write_text(
        "raise RuntimeError('this tree does not import')\n", encoding="utf-8"
    )
    ok, detail = cli._preflight_import(broken)
    assert not ok
    assert "this tree does not import" in detail


def test_the_preflight_covers_the_modules_a_fresh_process_loads():
    """`policy` by name: it is the module the 2026-08-18 measurement names, and
    a preflight that imported only `autoloop` would pass for a tree whose
    policy module does not load."""
    assert "autoloop.policy" in cli.PREFLIGHT_MODULES
    assert "autoloop.cli" in cli.PREFLIGHT_MODULES
    assert not [m for m in cli.PREFLIGHT_MODULES if "playwright" in m or "codex" in m], (
        "optional third-party deps would fail every preflight on a machine "
        "without them and disable the feature for good"
    )


# --- one shot: a replacement that dies early is not retried ------------------


def test_a_replacement_that_dies_before_one_iteration_is_not_retried(tmp_path, monkeypatch):
    """The successor's first boundary sees `execed`, not `pending`. A merge that
    imports but fails at RUNTIME must not produce a restart loop."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    lock = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    recording_execv(monkeypatch)

    with pytest.raises(Execed):
        cli._self_upgrade_at_boundary(config, lock)

    # The replacement died here — before any iteration completed. Everything a
    # fresh process would do with this record:
    assert store.load().status == UPGRADE_EXECED
    assert cli._self_upgrade_at_boundary(config, lock) == "none"
    orch, orch_config = orchestrator_at(tmp_path, Phase.READY, upgrades=True)
    UpgradeStore(orch_config.pending_upgrade_file).save(store.load())
    assert orch.run(max_steps=0) == Phase.READY.value


class StopLoop(Exception):
    """Ends `_run_continuous` from inside its idle sleep."""


def test_one_completed_iteration_retires_the_one_shot(tmp_path, monkeypatch):
    """And not before. The first iteration still finds the marker armed — that
    is the whole guarantee — and the second one, having proved the merged tree
    loads, reads config, state and registry and comes back, clears it."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record(status=UPGRADE_EXECED))
    observed: list = []

    monkeypatch.setattr(cli, "_select_and_kickoff", lambda *a, **k: False)

    def fake_sleep(seconds):
        # Only the idle poll counts as "an iteration finished". Matched by
        # duration so an unrelated internal sleep (a file-lock retry, say)
        # cannot be mistaken for one and shift the observations by a step.
        if seconds != cli.CONTINUOUS_POLL_SECONDS:
            return
        observed.append(store.load())
        if len(observed) >= 2:
            raise StopLoop()

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    args = argparse.Namespace(config=None, continuous=True, null_executor=True)

    with pytest.raises(StopLoop):
        cli._run_continuous(args, config)

    assert observed[0] is not None and observed[0].status == UPGRADE_EXECED
    assert observed[1] is None, "one full iteration under the merged code retires it"
    assert entries(config, "self_upgrade_confirmed")


def test_a_refused_exec_is_never_confirmed_as_a_replacement_that_happened(
    tmp_path, monkeypatch
):
    """The refusal, driven through the ITERATION that follows it.

    `os.execv` raising leaves this process running the code it started with, so
    `_run_continuous` carries on and reaches the top of the next iteration —
    the one place `_confirm_self_upgrade` fires. A record still saying `execed`
    there is retired with a `self_upgrade_confirmed` entry claiming an
    iteration completed under the merged code, when no replacement ever
    happened and that iteration was the OLD image's own. So the refusal puts
    the record back to `pending` itself, and the confirmation at the top of
    iteration two finds nothing to retire — and the upgrade survives for a
    process that can perform it.
    """
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())
    state = LoopState.new(URL)
    state.phase = Phase.READY.value
    StateStore(config.state_file).save(state)
    lock = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))

    def refuse(path, argv):
        raise OSError("Exec format error")

    monkeypatch.setattr(os, "execv", refuse)
    rounds: list = []

    class BoundaryThenStop:
        """The orchestrator's part, and only that: round one is the boundary
        that offers the upgrade, round two ends the test from inside iteration
        two — after the confirmation check at its top has already run, which is
        what makes `rounds[1]` evidence about that check."""

        def __init__(self, state):
            self.state = state
            self.declined: list = []

        def run(self, *_args, **_kwargs):
            rounds.append(store.load())
            if len(rounds) >= 2:
                raise StopLoop()
            return SELF_UPGRADE

        def decline_self_upgrade(self, base_sha):
            # `_run_continuous` re-declines what this run has already answered
            # onto each rebuilt orchestrator; recorded so the assertion below
            # can show the settled sha really was carried across the rebuild.
            self.declined.append(base_sha)
            return True

    built: list = []

    def build(config_, args_, store_, state_, task_store_, registry_):
        built.append(BoundaryThenStop(state_))
        return built[-1]

    monkeypatch.setattr(cli, "_build_orchestrator", build)
    args = argparse.Namespace(config=None, continuous=True, null_executor=True)

    with pytest.raises(StopLoop):
        cli._run_continuous(args, config, lock)

    assert len(rounds) == 2, "the process is still here — it ran a second iteration"
    assert entries(config, "self_upgrade_exec"), "the replacement was really attempted"
    assert rounds[1] is not None, (
        "the confirmation at the top of iteration two must not have cleared it: "
        "there was no replacement to confirm"
    )
    assert rounds[1].status == UPGRADE_PENDING
    assert store.load().status == UPGRADE_PENDING, (
        "and it stays RETRYABLE — the next process to reach a boundary on this "
        "state dir performs the upgrade this one could not"
    )
    assert entries(config, "self_upgrade_confirmed") == [], (
        "an entry here would record a replacement that did not happen"
    )
    assert entries(config, f"self_upgrade_{UPGRADE_EXEC_FAILED}"), "reported instead"
    assert built[1].declined == ["b" * 40], (
        "and the sha this run has already answered is re-declined onto the "
        "orchestrator iteration two builds — with the record still `pending` "
        "that carry-across is the ONLY thing standing between a retryable "
        "upgrade and a boundary offered every round forever"
    )


def test_confirmation_leaves_a_pending_record_alone(tmp_path):
    """A `pending` record describes an upgrade that has NOT happened — clearing
    it on someone else's completed iteration would drop the restart entirely."""
    config = make_config(tmp_path)
    store = UpgradeStore(config.pending_upgrade_file)
    store.save(pending_record())

    assert cli._confirm_self_upgrade(config) is False
    assert store.load().status == UPGRADE_PENDING


# --- the lock is continuously valid across the replacement -------------------


def lock_file(tmp_path) -> Path:
    return tmp_path / LOCK_FILENAME


def test_the_lock_never_lapses_across_the_replacement(tmp_path):
    """`os.execv` preserves the pid and runs no `finally`, so the lock file is
    never released and never has to be. What the successor image needs is to be
    able to ADOPT it — otherwise it finds a live lock (its own pid) and fails
    closed on itself."""
    first = LoopLock(tmp_path).acquire()
    original = first.read()
    assert LoopLock.is_live(original)

    assert first.mark_exec_handoff("self_upgrade abc123") is True
    armed = first.read()
    assert lock_file(tmp_path).exists() and LoopLock.is_live(armed)
    assert armed.pid == os.getpid() and armed.run_id == first.run_id
    # The token is armed in the environment too — that is what `os.execv`
    # carries to the successor, and it is the half no file can substitute for.
    assert inherited_token() == armed.exec_handoff["token"]

    # ---- os.execv happens here: same pid, new interpreter, new LoopLock ----
    successor = LoopLock(tmp_path).acquire()

    after = successor.read()
    assert lock_file(tmp_path).exists(), "the lock existed at every instant"
    assert LoopLock.is_live(after)
    assert after.pid == os.getpid()
    assert after.run_id == successor.run_id != original.run_id
    assert after.started_at == original.started_at, (
        "the lock really has been held since then — same pid, no gap"
    )
    assert after.exec_handoff is None, "one-shot: the marker is spent on adoption"
    assert inherited_token() is None, (
        "and so is the token: adoption consumes it, so nothing this process "
        "later spawns inherits an authorization that has already been used"
    )
    assert successor.adopted_run_id == original.run_id
    successor.release()


def test_the_successor_adopts_the_lock_the_boundary_armed(tmp_path, monkeypatch):
    """The same continuity, driven through the production arming site rather
    than by calling `mark_exec_handoff` directly — `cli._self_upgrade_at_
    boundary` is the only caller, so a handoff it arms is the only one that
    ever exists. The exec is recorded instead of taken; everything after it is
    what the replacement image does on its way to its first round."""
    config = make_config(tmp_path)
    UpgradeStore(config.pending_upgrade_file).save(pending_record())
    first = held_lock(config)
    monkeypatch.setattr(cli, "_preflight_import", lambda root: (True, ""))
    recording_execv(monkeypatch)

    with pytest.raises(Execed):
        cli._self_upgrade_at_boundary(config, first)

    # ---- os.execv happens here: same pid, new interpreter, new LoopLock ----
    successor = LoopLock(config.state_dir).acquire()

    assert successor.adopted_run_id == first.run_id
    assert (config.state_dir / LOCK_FILENAME).exists()
    assert successor.read().exec_handoff is None and inherited_token() is None
    with pytest.raises(LockHeldError):
        LoopLock(config.state_dir).acquire()
    successor.release()


def test_the_handoff_can_be_adopted_only_once(tmp_path):
    """Otherwise a later, unrelated process that happened to be given this pid
    could walk straight past a live lock."""
    first = LoopLock(tmp_path).acquire()
    first.mark_exec_handoff("once")
    LoopLock(tmp_path).acquire()

    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()


def test_a_live_lock_without_a_handoff_is_still_refused(tmp_path):
    """The rule is a HANDOFF, not "same pid may take the lock". Without this,
    adoption would be a lock-stealing hole rather than a continuity mechanism."""
    LoopLock(tmp_path).acquire()

    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()


def test_a_handoff_naming_another_pid_is_refused(tmp_path, monkeypatch):
    """Pids are reused. A marker that does not name THIS process is not this
    process's handoff, whoever wrote it.

    Everything else here is correct — host, run id, and a token this process
    really did inherit — so the refusal is attributable to the pid alone."""
    lock = LoopLock(tmp_path).acquire()
    info = lock.read()
    monkeypatch.setenv(EXEC_HANDOFF_TOKEN_ENV, "t" * 64)
    write_lock(
        tmp_path,
        info,
        {
            "pid": os.getpid() + 1,
            "run_id": info.run_id,
            "at": "now",
            "token": "t" * 64,
        },
    )

    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()


def test_a_foreign_hosts_handoff_is_refused(tmp_path, monkeypatch):
    """A pid means nothing across machines, and a lock we cannot verify is
    treated as live — the module's existing fail-closed rule, unchanged. Again
    the token matches, so the hostname is the only thing under test."""
    lock = LoopLock(tmp_path).acquire()
    info = lock.read()
    monkeypatch.setenv(EXEC_HANDOFF_TOKEN_ENV, "t" * 64)
    write_lock(
        tmp_path,
        LockInfo(
            pid=info.pid,
            hostname="some-other-host",
            started_at=info.started_at,
            run_id=info.run_id,
            state_dir=info.state_dir,
        ),
        {"pid": os.getpid(), "run_id": info.run_id, "at": "now", "token": "t" * 64},
    )

    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()


def test_a_handoff_naming_another_run_is_refused(tmp_path, monkeypatch):
    """The marker describes a handoff OF THIS LOCK, so it has to name the run
    the lock itself records. A marker copied from an older lock — or written
    against a run that has since released and re-acquired — describes something
    that is not the lock in front of us."""
    lock = LoopLock(tmp_path).acquire()
    info = lock.read()
    monkeypatch.setenv(EXEC_HANDOFF_TOKEN_ENV, "t" * 64)
    write_lock(
        tmp_path,
        info,
        {
            "pid": os.getpid(),
            "run_id": "a-different-run",
            "at": "now",
            "token": "t" * 64,
        },
    )

    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()


# --- the token: the marker's other facts are all forgeable -------------------


def test_a_valid_looking_marker_this_process_inherited_no_token_for_is_refused(tmp_path):
    """**The hardening, stated as its own claim.** Everything the marker can
    assert about itself is readable or reproducible from outside: the hostname
    is public, the run id is in the lock file next to it, and the pid is a
    small integer the kernel reuses within a boot. So a marker left on disk by
    a run that died — or written by anything that can write the state dir —
    plus that pid coming round again would be a complete handoff on the old
    rule. It is refused here because this process inherited no token, which is
    the one thing a stale file cannot supply, and it is refused as
    `LockHeldError`: the pid really is alive (it is ours), so the lock is live,
    and a live lock is never stolen."""
    lock = LoopLock(tmp_path).acquire()
    info = lock.read()
    assert inherited_token() is None, "no handoff was armed in this process"
    write_lock(
        tmp_path,
        info,
        {
            "pid": os.getpid(),
            "run_id": info.run_id,
            "at": utcnow_iso(),
            "reason": "self_upgrade deadbeef1234",
            "token": "the-token-this-marker-claims",
        },
    )

    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()
    assert lock.read().exec_handoff is not None, "and nothing was consumed"


def test_a_handoff_whose_token_is_not_the_inherited_one_is_refused(tmp_path, monkeypatch):
    """A real armed handoff, and a process holding some OTHER token. Guessing
    is the attack this closes, so a near miss has to be a miss."""
    lock = LoopLock(tmp_path).acquire()
    assert lock.mark_exec_handoff("armed") is True
    real = lock.read().exec_handoff["token"]
    monkeypatch.setenv(EXEC_HANDOFF_TOKEN_ENV, "0" * len(real))

    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()


def test_the_token_is_unguessable_and_travels_only_in_the_environment(tmp_path):
    """32 random bytes, minted per arming, and identical in exactly two places:
    the lock file and this process's environment.

    The second half is what pins "not derived": the two armings below share a
    lock, a pid, a run id and a reason, so a token computed from any of those
    would come out the same both times. Anything derivable is reproducible by
    whoever can read the lock, which is the whole point of not doing that."""
    lock = LoopLock(tmp_path).acquire()
    assert lock.mark_exec_handoff("self_upgrade abc123") is True
    first = lock.read().exec_handoff["token"]

    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)
    assert inherited_token() == first

    lock.clear_exec_handoff()
    assert lock.mark_exec_handoff("self_upgrade abc123") is True
    assert lock.read().exec_handoff["token"] != first, (
        "a fresh token per arming — a reused one would make the previous "
        "handoff's environment usable against this lock, and a derived one "
        "would be reproducible by anything that can read the lock file"
    )


@pytest.mark.parametrize(
    "recorded, inherited",
    [
        (42, "t" * 64),                 # not a string at all
        (None, "t" * 64),
        ({"token": "t"}, "t" * 64),
        # The sharpest case: `compare_digest("", "")` is a MATCH, so without
        # the emptiness guard an unset-looking token would adopt the lock.
        ("", ""),
        ("tökén-with-non-ascii", "tökén-with-non-ascii"),
    ],
)
def test_a_malformed_token_is_a_refusal_not_a_crash(tmp_path, monkeypatch, recorded, inherited):
    """`secrets.compare_digest` raises `TypeError` on a non-`str` and on any
    non-ASCII `str`, and both sides of this comparison come from outside the
    process — one off disk, one out of the environment. A raise would surface
    inside `acquire`, which in production is the successor's FIRST act after
    `execv`: a crash with no `finally` behind it, in place of the refusal this
    module answers everything it cannot verify with."""
    lock = LoopLock(tmp_path).acquire()
    info = lock.read()
    monkeypatch.setenv(EXEC_HANDOFF_TOKEN_ENV, inherited)
    write_lock(
        tmp_path,
        info,
        {"pid": os.getpid(), "run_id": info.run_id, "at": "now", "token": recorded},
    )

    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()


def test_arming_that_cannot_write_the_lock_leaves_no_token_behind(tmp_path, monkeypatch):
    """The environment is written first, so that a marker never reaches disk
    without its token being inheritable. The failing direction has to be tidied
    up by hand: a token left set would be inherited by every subprocess this
    run goes on to spawn, authorizing nothing today but outliving the reason it
    existed."""
    lock = LoopLock(tmp_path).acquire()
    reached: list = []

    def refuse(_self, _info):
        reached.append("write")
        raise OSError("read-only state dir")

    monkeypatch.setattr(LoopLock, "_write", refuse)

    assert lock.mark_exec_handoff("armed") is False
    assert reached == ["write"], (
        "the refusal has to come from the WRITE. `mark_exec_handoff` returns "
        "False from two guards above it as well, and either one would satisfy "
        "the assertions below while never minting a token at all"
    )
    assert inherited_token() is None
    assert lock.read().exec_handoff is None


def test_disarming_the_handoff_restores_the_ordinary_refusal(tmp_path):
    """The `execv`-was-refused path: no successor is coming, so nothing may
    adopt this lock afterwards. BOTH halves go — the marker on disk and the
    token in the environment, the latter because it would otherwise be
    inherited by every subprocess this run spawns from here on."""
    lock = LoopLock(tmp_path).acquire()
    lock.mark_exec_handoff("armed")
    lock.clear_exec_handoff()

    assert lock.read().exec_handoff is None
    assert inherited_token() is None
    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()


def test_the_superseded_lock_object_cannot_delete_the_successors_lock(tmp_path):
    """The pre-exec `LoopLock` is gone with its process image in production, but
    a refused exec leaves it alive in this one. Its `release` must still be
    governed by the run-id check that already exists."""
    first = LoopLock(tmp_path).acquire()
    first.mark_exec_handoff("armed")
    successor = LoopLock(tmp_path).acquire()

    first.release()

    assert lock_file(tmp_path).exists()
    assert successor.read().run_id == successor.run_id
    successor.release()


def test_arming_a_lock_we_do_not_own_is_refused(tmp_path):
    """`mark_exec_handoff` returning False is what stops the replacement, so it
    must be false for every state that is not "we hold this lock right now"."""
    unheld = LoopLock(tmp_path)
    assert unheld.mark_exec_handoff("nope") is False
    assert not lock_file(tmp_path).exists()

    held = LoopLock(tmp_path).acquire()
    held.release()
    assert held.mark_exec_handoff("nope") is False


def test_the_lock_is_never_unlinked_while_being_rewritten(tmp_path, monkeypatch):
    """Both rewrites go temp-file + `os.replace`. An unlink would open exactly
    the window "no window for another process to take it" denies."""
    lock = LoopLock(tmp_path).acquire()

    def refuse(*_args, **_kwargs):
        raise AssertionError("the lock file must never be unlinked mid-handoff")

    monkeypatch.setattr(Path, "unlink", refuse)
    assert lock.mark_exec_handoff("armed") is True
    lock.clear_exec_handoff()
    assert lock_file(tmp_path).exists()


def test_lock_info_still_reads_a_lock_written_before_handoffs_existed(tmp_path):
    """Every lock on disk today has no `exec_handoff` key. Reading one must not
    become an error, or the first run after this change cannot inspect the lock
    it is complaining about."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock_file(tmp_path).write_text(
        json.dumps(
            {
                "pid": 1,
                "hostname": "old-host",
                "started_at": utcnow_iso(),
                "run_id": "legacy",
                "state_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    info = LoopLock(tmp_path).read()
    assert isinstance(info, LockInfo)
    assert info.exec_handoff is None
