"""An agent that reports its scope makes the task impossible is not told to
revise again (review-01b).

THE ONE CLAIM these tests grade: when an executor has disclosed, on an
`ASSUMPTION:` line, that the task cannot be done inside its approved paths, and
the reviewer answers `revise` on a record that has already spent
`MIN_REVIEWS_BEFORE_SCOPE_PARK` reviews, the loop parks
`approved_scope_blocks_task` instead of dispatching another round — and it does
so BEFORE any ceiling can reach the same record first.

The measured failure, brw-19a (2026-08-27): the agent disclosed in exactly that
form that three of four consumers it had to change were outside its approved
paths, and named the remedy it would have asked for. The reviewer answered
`revise` four times in escalating detail. Four rounds, four agents, every report
correct, and the task ended on `attempt_count_ceiling` — a code naming the
task's budget for a problem that was never about its budget.

Four things break the claim if left untested, and there is a section for each:

  * §1 RECOGNITION. The boundary half of the disclosure ("outside my approved
    paths") is what every correctly-scoped task writes about a file it left
    alone ON PURPOSE. Three such lines from this repository's own
    `docs/SUMMARY.md` are pinned here verbatim as negatives, because a
    recognizer that fires on them calls three FINISHED tasks impossible.
  * §2 THE PACKET. The notice has to survive the render budgets that drop the
    oldest assumptions first — which is exactly the entry a round-1 disclosure
    becomes — and it has to reach the executor-report section both packet
    builders actually embed, on both of that section's branches. It is asserted
    as `IMPOSSIBLE_SCOPE_NOTICE`'s own bytes, which is why the renderer emits
    the constant verbatim rather than re-indented.
  * §3 THE METER. It counts REVIEWS, not dispatches. A round the process did
    not survive is re-dispatched, and a meter reading `attempt_count` would
    count that round twice and report a first disclosure as a repeat.
  * §4 PRECEDENCE. Being correct but LATE is the whole defect: every ceiling
    below this guard ends the task under a code that blames a budget, and on
    the task this exists for the attempt ceiling is what got there first.

Sections 1-3's recognizer tests are pure functions over a record — no
repository, no subprocess, no agent, per CLAUDE.md's "cheapest test that can
fail for the right reason". Section 4 is real git and real worker repos, with
the harness duplicated per this suite's self-contained convention (the same
shape `test_task_split.py` uses), because precedence is a claim about the
dispatch path and nothing cheaper can fail for that reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from gitrepo import make_repo_from_template

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import NO_WANTED_DECISION, Decision, Directive, parse_response
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import _extract_assumptions
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    MAX_TASK_ATTEMPTS,
    MIN_REVIEWS_BEFORE_SCOPE_PARK,
    Orchestrator,
)
from autoloop.packet import (
    ASSUMPTIONS_MAX_CHARS,
    IMPOSSIBLE_SCOPE_NOTICE,
    _format_assumptions,
    _format_executor_report,
    impossible_scope_disclosures,
    reports_impossible_scope,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import (
    IntentStore,
    TaskExecution,
    TaskExecutionStore,
    retire_execution,
)

URL = "https://chatgpt.com/c/impossible-scope-test"

PATHS = ("docs/A.md", "docs/B.md")


# =============================================================================
# the fixtures the recognizer is graded against
# =============================================================================

#: THE POSITIVE, in the shape brw-19a reported: the boundary it hit, the work
#: that boundary forbids, and the remedy it would have asked for.
#:
#: RECONSTRUCTED from that task's description rather than copied out of its
#: transcript — the transcript is not in this repository, and the durable
#: record of the round is `docs/SUMMARY.md:60-61`, which is the SUMMARY note,
#: not the assumption line. What is load-bearing here is the SHAPE the brief
#: states (outside the approved paths + cannot be completed + the remedy), and
#: the three paraphrases below vary the wording so the pin is on that shape
#: rather than on one sentence somebody typed once.
BRW_19A_DISCLOSURE = (
    "three of the four consumers that pin these strings are outside this "
    "task's approved paths, so the deletion cannot be completed inside the "
    "approved scope; I would have asked for those three test files to be "
    "added to `approved_paths` before this task ran."
)

DISCLOSURE_PARAPHRASES = (
    BRW_19A_DISCLOSURE,
    "I assumed the narrower reading, but it is not possible to satisfy the "
    "task's own claim without editing `autoloop/blockers.py`, which is "
    "outside my approved paths — I would have asked for it to be added.",
    "the remaining half of this change is impossible inside the approved "
    "paths I was given: `docs/AUTOLOOP.md` holds the table and is not in "
    "them. I would have asked for a follow-up task that owns that file.",
    "there is no way to keep the two lists in agreement from inside my "
    "approved scope, because the second list lives in a file this task may "
    "not touch; I would have asked for the scope to be widened.",
)

#: THE NEGATIVES, verbatim from `docs/SUMMARY.md` (lines 58, 61 and 68 at
#: 047ca47). Every one of them names the approved paths as a boundary, every
#: one describes a real consequence of that boundary — and every one belongs to
#: a task that COMPLETED and was merged. brw-19a's own note is among them,
#: which is the sharpest form of the point: the same task both disclosed a
#: blocker and wrote a deliberate hand-off, and only the first is one.
BRW_19C_HANDOFF = (
    "LEFT STANDING as a hand-off, not an oversight: `orchestrator.py:3299`, "
    "`:11381` and `:11468` still tell an operator to run `python3 -m "
    "autoloop.browser.chrome_restart`. Prose, not an import, so nothing breaks "
    "until brw-19a deletes the package — but `autoloop/orchestrator.py` is not "
    "in brw-19c's approved paths, and the strings are pinned from "
    "`test_rounds_and_restart.py:1062,1069` (the second over `question[:160]`) "
    "and `test_transport_fault_recovery.py:908`, neither approved either. "
    "Rewording without all three files in one commit turns three green tests "
    "red. Recorded at `test_transport_vocabulary.py`."
)

BRW_19A_LEFT_STANDING = (
    "LEFT STANDING, deliberately: `autoloop/conversation.py:385` and "
    "`autoloop/config.py:364` still spell `python3 -m "
    "autoloop.browser.chrome_restart` inside COMMENTS — one quoting a park a "
    "`codex_cli` run wrongly wrote on 2026-08-22, one describing what replaced "
    "the retired `browser.restart_command` example. Prose, not imports, so "
    "nothing breaks; both files are outside brw-19a's approved paths. "
    "`codex/conversation.py:163`/`:345` still name the `browser_chatgpt` SEAT, "
    "retired by brw-16 and preserved on purpose. Recorded at "
    "`test_transport_vocabulary.py`."
)

BRW_19E_FOLLOW_UP = (
    "LEFT FOR A FOLLOW-UP TASK, both outside these approved paths: "
    "`orchestrator.py:3577` still advises setting "
    '`conversation.fallback_provider` "(the browser provider draws on a '
    'separate quota)", and `_handle_quota_exhausted` parks with '
    '`f"{exc} {extra}"`, so that clause is read as one paragraph with the '
    "message above it; `codex/conversation.py:345` makes the same claim in the "
    "codex_cli seat's own `QuotaExhaustedError`. The seat both of them point "
    "at has been unregistered since brw-16."
)

INTENTIONALLY_UNTOUCHED = (
    BRW_19C_HANDOFF,
    BRW_19A_LEFT_STANDING,
    BRW_19E_FOLLOW_UP,
    "I deliberately left `autoloop/cli.py` untouched. It is outside my "
    "approved paths and nothing in it needed to change for this claim to "
    "hold; I would have asked whether the operator surface should follow.",
    "I assumed the narrow reading of 'the tracker' and edited only "
    "`docs/TESTS.md`; `docs/AUTOLOOP.md` is out of scope and I left it alone.",
)


def execution_with(assumptions, **kwargs) -> TaskExecution:
    """A record carrying `assumptions` and nothing else that matters here."""
    return TaskExecution(
        task_id=kwargs.pop("task_id", "t1"),
        task_branch=kwargs.pop("task_branch", "autoloop/t1"),
        worktree_path=kwargs.pop("worktree_path", "/tmp/worker"),
        task_base_sha=kwargs.pop("task_base_sha", "0" * 40),
        assumptions=tuple(assumptions),
        **kwargs,
    )


# =============================================================================
# 1. recognition: the literal report format, and what it must NOT read as one
# =============================================================================


def test_the_brw_19a_report_shape_is_recognised():
    """The format is the thing to match. Each paraphrase carries the two halves
    the brief describes — the approved paths as the wall, and the work as
    impossible against it — in different words."""
    for line in DISCLOSURE_PARAPHRASES:
        assert reports_impossible_scope(line), line


def test_a_deliberately_untouched_file_is_not_a_blocker():
    """THE false positive, and three of the five fixtures are real notes from
    `docs/SUMMARY.md` written by tasks that finished.

    A file a round left alone on purpose is evidence the round was scoped
    correctly, not evidence the scope made the task impossible. Reading it as
    the second would park finished work."""
    for line in INTENTIONALLY_UNTOUCHED:
        assert not reports_impossible_scope(line), line
    assert impossible_scope_disclosures(INTENTIONALLY_UNTOUCHED) == ()


def test_the_two_halves_must_appear_in_the_SAME_disclosure():
    """Round 1 names the boundary about a file it left alone; round 2 says
    something unrelated cannot be done. Neither round reported an impossible
    scope, and scanning the accumulated list as one blob would invent a
    disclosure out of two lines that are each fine."""
    boundary_only = BRW_19A_LEFT_STANDING
    blocked_only = (
        "the golden fixture cannot be made here — regenerating it needs network "
        "access this worker does not have — so I hand-wrote it instead."
    )
    # Each half really is present in the pair, which is what makes this a test
    # of the SAME-LINE rule rather than of two lines that match nothing.
    assert any(
        phrase in boundary_only.lower()
        for phrase in ("approved path", "approved scope", "out of scope")
    )
    assert "cannot be made" in blocked_only
    assert impossible_scope_disclosures((boundary_only, blocked_only)) == ()


def test_an_ordinary_ambiguity_disclosure_is_not_an_impossible_scope_one():
    """The line this mechanism sits on top of, and the reason "cannot be
    resolved" is NOT one of the blocked phrases: it is what an agent writes
    about an AMBIGUITY, which is exactly what an `ASSUMPTION:` line is for. A
    recognizer admitting it would fire on the ordinary case and park tasks for
    doing the thing the brief asks of them."""
    ambiguous = (
        "the two readings cannot be resolved from the task text, so I took the "
        "narrower one; the wider one would also have touched files outside my "
        "approved paths, and I would have asked which was meant.",
        "I could not tell whether 'the tracker' meant `docs/TESTS.md` or every "
        "tracker, and the wider reading is out of scope, so I took the narrow "
        "one.",
    )
    for line in ambiguous:
        assert not reports_impossible_scope(line), line


def test_an_echo_of_the_instruction_is_not_a_disclosure():
    """The prompt tells every agent what an `ASSUMPTION:` line is for and shows
    the form. An agent that repeats the instruction, or leaves the placeholder
    in, must not thereby file a blocker: text GIVEN to a model is not evidence
    the model produced.

    The placeholder is the residual `_ASSUMPTION_RE` already accepts (a verbatim
    reproduction of the example line IS in the collected form), so it is pinned
    here at the next stage instead: collected, and still not a disclosure."""
    echoes = (
        "<what you assumed, and what you would have asked>",
        "Fix only the failures that fall INSIDE THIS TASK'S APPROVED SCOPE — "
        "the exact path list under APPROVED SCOPE below, which is the list the "
        "loop grades your diff against. A failure outside it you REPORT rather "
        "than fix, naming the file and what breaks.",
        "A failure anywhere else you REPORT, naming the file and what breaks.",
    )
    for line in echoes:
        assert not reports_impossible_scope(line), line


def test_a_markup_prefixed_line_is_never_collected_in_the_first_place():
    """The recognizer reads COLLECTED lines, so it inherits
    `_ASSUMPTION_RE`'s anchoring rather than restating it: an agent quoting the
    instruction as a bullet or a blockquote produces no assumption at all, and
    therefore no blocker, whatever the sentence says."""
    raw = (
        "Report:\n"
        f"- ASSUMPTION: {BRW_19A_DISCLOSURE}\n"
        f"> ASSUMPTION: {BRW_19A_DISCLOSURE}\n"
        f"* ASSUMPTION: {BRW_19A_DISCLOSURE}\n"
    )
    assert _extract_assumptions(raw) == ()
    assert impossible_scope_disclosures(_extract_assumptions(raw)) == ()

    # ...and the same sentence, declared properly, is collected and recognised.
    declared = f"  ASSUMPTION: {BRW_19A_DISCLOSURE}\n"
    collected = _extract_assumptions(declared)
    assert collected == (BRW_19A_DISCLOSURE,)
    assert impossible_scope_disclosures(collected) == (BRW_19A_DISCLOSURE,)


def test_a_wrapped_or_shouted_disclosure_is_still_one():
    """Agents wrap their own prose and vary their case. Whitespace collapse and
    case folding are what stop that being the difference between a park and
    four more rounds."""
    wrapped = BRW_19A_DISCLOSURE.replace(" so ", "\n   so\n   ").upper()
    assert reports_impossible_scope(wrapped)


def test_an_unreadable_assumption_record_reports_no_disclosure():
    """FAIL-OPEN BY CHOICE, and stated as one. A record that is not a list of
    strings tells the loop nothing, and a task must not be parked on evidence
    nobody can read — so the answer is "no disclosure", which leaves the loop
    behaving exactly as it did before this guard existed. What it must never do
    is raise, because it runs inside a dispatch."""
    for junk in (None, "", "a bare string", 0, {"a": 1}, object()):
        assert impossible_scope_disclosures(junk) == ()
    assert impossible_scope_disclosures([None, 7, BRW_19A_DISCLOSURE]) == (
        BRW_19A_DISCLOSURE,
    )
    for junk in (None, 7, b"bytes", []):
        assert not reports_impossible_scope(junk)


# =============================================================================
# 2. the packet says so, and keeps saying so as the list grows
# =============================================================================


def test_the_packet_flags_a_standing_disclosure_to_the_reviewer():
    """The reviewer is who kept answering `revise`, so the notice is addressed
    to the reviewer and names the answers that can actually move the task.

    Asserted as the CONSTANT'S OWN BYTES, which is why it is rendered
    unindented: a test that rebuilds the notice from whatever indent the
    section happens to use is pinning the renderer's whitespace, not the claim
    that the reviewer was told."""
    rendered = _format_assumptions(execution_with([BRW_19A_DISCLOSURE]))
    assert IMPOSSIBLE_SCOPE_NOTICE in rendered
    assert BRW_19A_DISCLOSURE in rendered


def test_the_packet_does_not_cry_wolf_over_an_ordinary_assumption():
    """Five ordinary hand-off assumptions, no notice — and the list is still
    rendered, so the absence is the notice's and not the section's.

    Asserted on a distinctive PREFIX rather than the whole fixture, because
    two of these are over `ASSUMPTION_MAX_CHARS_EACH` and are legitimately
    shortened by the render bound this section already had."""
    rendered = _format_assumptions(execution_with(INTENTIONALLY_UNTOUCHED))
    assert IMPOSSIBLE_SCOPE_NOTICE not in rendered
    assert "LEFT STANDING, deliberately:" in rendered
    assert "LEFT FOR A FOLLOW-UP TASK" in rendered


def test_the_notice_survives_the_disclosure_being_dropped_from_the_render():
    """The render budget drops the OLDEST entries first, and a round-1
    disclosure is the oldest entry by the time it has been repeated at. If the
    notice were computed over what got rendered, it would switch itself off
    exactly as the evidence for it accumulated."""
    filler = [f"a later, ordinary assumption, number {n}. " * 10 for n in range(40)]
    execution = execution_with([BRW_19A_DISCLOSURE, *filler])
    rendered = _format_assumptions(execution)

    assert sum(len(f) for f in filler) > ASSUMPTIONS_MAX_CHARS, "filler too small"
    assert BRW_19A_DISCLOSURE not in rendered, "the fixture no longer overflows"
    assert "not shown here" in rendered
    assert IMPOSSIBLE_SCOPE_NOTICE in rendered


def test_the_notice_reaches_the_section_a_packet_actually_carries():
    """The rendering above is a helper; what a reviewer receives is the
    executor-report section, and BOTH packet builders embed exactly that.

    Its two branches are asserted together because the second is the one a
    refactor drops: a record whose report is empty — a candidate from an older
    build, or one adopted after a crash — takes an early return, and the
    assumptions (which accumulate across rounds and so outlive the report that
    was replaced) have to be concatenated onto it too. A notice that survived
    only the ordinary branch would go missing on exactly the records that have
    been through the most rounds."""
    reported = execution_with(
        [BRW_19A_DISCLOSURE], report_summary="did the reachable half", report_details="d"
    )
    assert IMPOSSIBLE_SCOPE_NOTICE in _format_executor_report(reported)

    silent = execution_with([BRW_19A_DISCLOSURE])
    assert silent.report_summary == "" and silent.report_details == ""
    rendered = _format_executor_report(silent)
    assert "none recorded" in rendered, "not the empty-report branch any more"
    assert IMPOSSIBLE_SCOPE_NOTICE in rendered

    # The control: the same two branches with an ordinary hand-off note carry
    # the note and no notice, so neither assertion above can be passing on a
    # notice this section prints unconditionally.
    for control in (
        execution_with([BRW_19A_LEFT_STANDING], report_summary="s", report_details="d"),
        execution_with([BRW_19A_LEFT_STANDING]),
    ):
        rendered = _format_executor_report(control)
        assert "LEFT STANDING, deliberately:" in rendered
        assert IMPOSSIBLE_SCOPE_NOTICE not in rendered


# =============================================================================
# 3. the meter counts reviews, and counts a re-entered round once
# =============================================================================


def test_the_meter_reads_the_counter_that_cannot_double_count():
    """`review_round` moves where a packet becomes the outbox;
    `carried_review_rounds` holds what a carry-forward reset off it. Neither
    moves on a dispatch that sends no packet — which is what `attempt_count`
    and the ledger deliberately do."""
    execution = execution_with(
        [], review_round=1, carried_review_rounds=2, attempt_count=5
    )
    assert Orchestrator._reviews_delivered(execution) == 3


def test_a_non_revise_directive_is_never_refused_by_this_guard():
    """It refuses ONE verb. `push`, `stop`, `recut` and a ceiling `plan` all
    still reach a record carrying a disclosure — otherwise the guard would
    strand the very task it is trying to unstick."""
    orch = Orchestrator.__new__(Orchestrator)
    execution = execution_with([BRW_19A_DISCLOSURE], review_round=4)
    for decision in (Decision.PUSH, Decision.STOP, Decision.PLAN, Decision.IMPLEMENT):
        directive = Directive(decision=decision, reason="r", task_id="t1")
        assert orch._revise_cannot_help(execution, directive) == ()

    revise = Directive(decision=Decision.REVISE, reason="r", task_id="t1", feedback="f")
    assert orch._revise_cannot_help(execution, revise) == (BRW_19A_DISCLOSURE,)


def test_a_first_disclosure_is_not_a_repeat():
    """The boundary, from below. One review spent means the reviewer has
    answered once; that answer is allowed to be a `revise`, because it may name
    a remedy that dissolves the disclosure."""
    orch = Orchestrator.__new__(Orchestrator)
    revise = Directive(decision=Decision.REVISE, reason="r", task_id="t1", feedback="f")
    for reviews in range(MIN_REVIEWS_BEFORE_SCOPE_PARK):
        execution = execution_with([BRW_19A_DISCLOSURE], review_round=reviews)
        assert orch._revise_cannot_help(execution, revise) == ()

    at_threshold = execution_with(
        [BRW_19A_DISCLOSURE], review_round=MIN_REVIEWS_BEFORE_SCOPE_PARK
    )
    assert orch._revise_cannot_help(at_threshold, revise) == (BRW_19A_DISCLOSURE,)


def test_the_accepted_residual_is_pinned_rather_than_left_to_be_discovered():
    """A task whose FIRST disclosure lands in its SECOND round parks on that
    disclosure rather than after it, and this test says so on purpose.

    The meter counts REVIEWS because nothing durable records which round an
    assumption came from — `TaskExecution.assumptions` is accumulated and
    deduplicated, so round 2's new line and round 1's restated one are the same
    entry here. Closing it needs a per-round field on the execution record,
    which lives in `worktask.py`.

    It is pinned as the CURRENT answer, not as the desired one: a later change
    that gains that field should make this test fail and be rewritten, rather
    than quietly shifting behaviour nobody wrote down."""
    orch = Orchestrator.__new__(Orchestrator)
    revise = Directive(decision=Decision.REVISE, reason="r", task_id="t1", feedback="f")
    # Round 1 assumed something ordinary; round 2 was the first to hit the wall.
    late = execution_with([INTENTIONALLY_UNTOUCHED[3], BRW_19A_DISCLOSURE], review_round=2)
    assert orch._revise_cannot_help(late, revise) == (BRW_19A_DISCLOSURE,)


# =============================================================================
# 4. precedence: real dispatches, and the ceilings that used to get there first
# =============================================================================


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def block(obj) -> str:
    obj = dict(obj)
    obj.setdefault("wanted_decision", NO_WANTED_DECISION)
    return "Reasoning...\n```json\n" + json.dumps(obj) + "\n```"


FIRST_PLAN = {
    "approach": "one commit",
    "files": ["docs/A.md"],
    "steps": ["write the file"],
}


def implement_block(task_id="t1"):
    return block(
        {
            "version": 3,
            "decision": "implement",
            "reason": "next",
            "task_id": task_id,
            "decomposition": FIRST_PLAN,
        }
    )


def revise_block(task_id="t1", feedback="fix it"):
    return block(
        {
            "version": 3,
            "decision": "revise",
            "reason": "needs work",
            "task_id": task_id,
            "feedback": feedback,
        }
    )


class FakeClient:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        self.closed = False

    def attach(self):
        pass

    def has_request(self, request_id):
        return request_id in self.persisted

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        from autoloop.conversation import SubmitResult

        self.submitted.append((request_id, prompt))
        self.persisted.add(request_id)
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        if not self.responses:
            raise AssertionError("test script exhausted: no response left")
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        self.closed = True


class ScriptedExecutor:
    """Writes into the dispatched task's worker repo and reports success,
    unless this round's entry in `rounds` says otherwise."""

    def __init__(self, workers_root, rounds=()):
        self.workers_root = Path(workers_root)
        self.rounds = list(rounds)
        self.calls: list[tuple] = []

    def execute(self, directive, task):
        self.calls.append((directive, task))
        index = len(self.calls) - 1
        if index < len(self.rounds) and self.rounds[index] is not None:
            return self.rounds[index]
        worker = self.workers_root / task.id
        target = worker / "docs/A.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# round {len(self.calls)}\n", encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary="did it",
            details="details",
            validation="ruff clean",
            changed_paths=("docs/A.md",),
        )


FAILED_ROUND = ExecutionOutcome(
    status="error",
    summary="task 't1': validation failed",
    details="1 test failed",
    validation="failed",
)


@dataclass
class Wiring:
    orch: Orchestrator
    registry: TaskRegistry
    execution_store: TaskExecutionStore
    worker_repos: WorkerRepoManager
    executor: ScriptedExecutor
    config: AutoloopConfig


def build(tmp_path, responses=(), rounds=(), policy=None) -> Wiring:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    make_repo_from_template(repo_root, branch="main", files=(("README.md", "hi\n"),))

    policy_config = policy or PolicyConfig(implement_enabled=True)
    git = GitGateway(repo_root, PolicyEngine(policy_config))
    worker_repos = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=policy_config,
        state_dir=tmp_path / ".al",
    )
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry(
        [Task(id="t1", title="T", description="d", approved_paths=PATHS)]
    )
    task_store.save(registry)

    state = LoopState.new(URL)
    state.outbox = "kickoff report"
    store = StateStore(config.state_file)
    store.save(state)

    executor = ScriptedExecutor(worker_repos.root_dir, rounds=rounds)
    client = FakeClient(responses)
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=IntentStore(tmp_path / "intents"),
        validation_runner=ok_validation,
    )
    return Wiring(
        orch=orch,
        registry=registry,
        execution_store=execution_store,
        worker_repos=worker_repos,
        executor=executor,
        config=config,
    )


def records(wiring, kind):
    path = wiring.config.transcript_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == kind:
            out.append(entry.get("data") or {})
    return out


def park_codes(wiring):
    return [record.get("code") for record in records(wiring, "needs_user")]


def dispatch(wiring, response_text):
    wiring.orch.state.last_response = None
    wiring.orch._dispatch(parse_response(response_text))


def first_round(tmp_path, rounds=(), policy=None) -> Wiring:
    """A wiring whose task `t1` has been implemented once: an execution record,
    a candidate, a stored plan and `review_round == 1`."""
    wiring = build(
        tmp_path, responses=[implement_block("t1")], rounds=rounds, policy=policy
    )
    wiring.orch.run(max_steps=4)
    assert wiring.execution_store.load("t1").review_round == 1
    return wiring


def disclose(wiring, *, reviews=MIN_REVIEWS_BEFORE_SCOPE_PARK, lines=None, **fields):
    """Put the record in the state this guard is about: a standing disclosure
    and `reviews` reviews already spent.

    The counters are SET rather than earned, like `test_task_split.
    spend_to_ceiling` and for the same reason — four real rounds would prove
    nothing this file is about and would cost four agent rounds to do it. What
    matters here is the state at the moment the reviewer answers."""
    execution = wiring.execution_store.load("t1")
    execution.review_round = reviews
    if lines is None:
        lines = [BRW_19A_DISCLOSURE]
    execution.assumptions = tuple(lines)
    for name, value in fields.items():
        setattr(execution, name, value)
    wiring.execution_store.save(execution)
    return execution


def test_a_revise_on_a_standing_disclosure_parks_instead_of_dispatching(tmp_path):
    """THE claim, in its narrowest form: no agent runs, nothing is rolled back,
    and the park quotes the executor's own sentence rather than a paraphrase of
    it."""
    wiring = first_round(tmp_path)
    disclose(wiring)
    before = len(wiring.executor.calls)

    dispatch(wiring, revise_block("t1", feedback="please handle the other three"))

    state = wiring.orch.state
    assert state.phase == Phase.NEEDS_USER.value
    assert state.park_kind == "task_fatal"
    assert park_codes(wiring) == ["approved_scope_blocks_task"]
    assert BRW_19A_DISCLOSURE in (state.question or "")
    assert len(wiring.executor.calls) == before, "an agent was dispatched anyway"
    # The candidate is untouched: nothing was rolled back, nothing committed.
    execution = wiring.execution_store.load("t1")
    assert execution.candidate_sha
    assert execution.attempt_count == 1, "the refused round must spend no attempt"


def test_the_guard_fires_before_the_attempt_ceiling(tmp_path):
    """Fault 3, and the shape brw-19a actually died in: four `revise` rounds
    reached the attempt ceiling, so a guard placed after it would have been
    correct and never reached."""
    wiring = first_round(tmp_path)
    disclose(wiring, attempt_count=MAX_TASK_ATTEMPTS)

    dispatch(wiring, revise_block("t1", feedback="again, with feeling"))

    assert park_codes(wiring) == ["approved_scope_blocks_task"]
    # ...and the ceiling machinery never ran: no classification was requested,
    # which is what `_handle_attempt_ceiling` would have done first.
    assert wiring.registry.get("t1").ceiling_plan_requested_at == ""


def test_without_the_disclosure_that_same_record_still_meets_the_ceiling(tmp_path):
    """The control for the test above. The ceiling is not removed, disabled or
    reordered — it is preempted for exactly one shape of record."""
    wiring = first_round(tmp_path)
    disclose(wiring, lines=[BRW_19A_LEFT_STANDING], attempt_count=MAX_TASK_ATTEMPTS)

    dispatch(wiring, revise_block("t1", feedback="again, with feeling"))

    assert park_codes(wiring) == []
    assert wiring.registry.get("t1").ceiling_plan_requested_at, (
        "the attempt ceiling should have asked the reviewer to classify"
    )


def test_the_park_is_not_a_dead_end_once_the_record_is_retired(tmp_path):
    """The operator route the park text names, exercised.

    The disclosure lives on the EXECUTION RECORD, which accumulates across
    rounds and is never re-derived from the task — so widening `approved_paths`
    alone leaves it standing and the next `revise` parks again. `discard` and
    `release` both retire the record through `worktask.retire_execution`, and
    that is what lets the wider scope take effect. Without this the guard would
    be a wall rather than a redirection, and the park text would be wrong.
    """
    wiring = first_round(tmp_path)
    disclose(wiring)
    dispatch(wiring, revise_block("t1", feedback="please handle the other three"))
    assert park_codes(wiring) == ["approved_scope_blocks_task"]
    parked_after = len(wiring.executor.calls)

    retire_execution(
        "t1", wiring.execution_store, wiring.worker_repos, reason="scope-widened"
    )
    dispatch(wiring, revise_block("t1", feedback="now with the wider scope"))

    assert park_codes(wiring) == ["approved_scope_blocks_task"], "a NEW park appeared"
    assert len(wiring.executor.calls) == parked_after + 1, "the round did not run"
    assert wiring.execution_store.load("t1").assumptions == ()


def test_the_guard_fires_before_the_review_round_cap(tmp_path):
    """The other ceiling, and the same argument: `review_round_cap` reports
    that the task ran out of rounds, which is true and is not the reason."""
    policy = PolicyConfig(implement_enabled=True, max_review_rounds=2)
    wiring = first_round(tmp_path, policy=policy)
    disclose(wiring, reviews=2)

    dispatch(wiring, revise_block("t1", feedback="round three, please"))

    assert park_codes(wiring) == ["approved_scope_blocks_task"]


def test_without_the_disclosure_that_same_record_still_meets_the_round_cap(tmp_path):
    """The control for the test above."""
    policy = PolicyConfig(implement_enabled=True, max_review_rounds=2)
    wiring = first_round(tmp_path, policy=policy)
    disclose(wiring, reviews=2, lines=[BRW_19E_FOLLOW_UP])

    dispatch(wiring, revise_block("t1", feedback="round three, please"))

    assert park_codes(wiring) == ["review_round_cap"]


def test_a_round_the_loop_had_to_redo_does_not_become_a_second_review(tmp_path):
    """Fault 2, end to end. The middle dispatch runs an agent, spends an
    attempt and sends NO packet — the shape a crash, a killed agent or a failed
    validation all leave behind. A meter reading `attempt_count` would call the
    third dispatch a repeat and park a FIRST disclosure; the review counter
    does not move, so the round runs."""
    wiring = first_round(tmp_path, rounds=(None, FAILED_ROUND))
    disclose(wiring, reviews=1)

    dispatch(wiring, revise_block("t1", feedback="first ask"))
    execution = wiring.execution_store.load("t1")
    assert Orchestrator._reviews_delivered(execution) == 1, "no packet was sent"
    assert len(execution.attempt_ledger) == 2, "but the dispatch was charged"

    dispatch(wiring, revise_block("t1", feedback="second, different ask"))

    # Asserted on THIS code rather than on an empty park list: what the failed
    # round in between does otherwise is another mechanism's claim, and pinning
    # it here would make this test fail for a reason it is not about.
    assert "approved_scope_blocks_task" not in park_codes(wiring), (
        "a first disclosure was read as a repeat"
    )
    assert len(wiring.executor.calls) == 3, "the round did not run"
    # And once that round really is reviewed, the next revise is the repeat.
    execution = wiring.execution_store.load("t1")
    assert Orchestrator._reviews_delivered(execution) == 2

    dispatch(wiring, revise_block("t1", feedback="third, still different"))

    assert park_codes(wiring)[-1] == "approved_scope_blocks_task"
