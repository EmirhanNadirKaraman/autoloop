"""wanted-01: every directive answers which verb the reviewer wanted, including
when the answer is "the one I used".

THE CLAIM, in one sentence: a directive without `wanted_decision` is DENIED by
`policy.authorize_directive` and draws the budget-capped corrective re-prompt —
the parser still accepts its absence and `PROTOCOL_VERSION` stays 3 — and the
schema text asks for it on EVERY reply rather than only when nothing fits.

WHY IT NEEDED CHANGING. `Directive.wanted_decision`'s own comment says the tally
is how the next missing verb gets found "by counting instead of by someone
happening to read a `reason` field". Measured 2026-08-25 it had produced not one
data point: ZERO uses across every directive, and `wanted_decisions_file` did not
exist because nothing had ever written to it. The field was not being ignored —
it was barely being asked. The schema posed the question only "when none above
fits", a condition that effectively never holds, so a zero tally read as "the
vocabulary is complete" when nobody had actually been asked. Meanwhile brw-14
PASSED review on 2026-08-24 and parked on `review_packet_build_failed` with a
416,193-byte range diff, and five task descriptions written that day carry a
hand-written "produce a split plan if this is too large" — the operator writing
by hand what the reviewer had no way to say.

FOUR CONSTRAINTS, each a way this can fail, and each with tests below:

* `PROTOCOL_VERSION` stays 3 and the parser still accepts an omitted field —
  "the parser is unchanged" below;
* a missing answer NEVER becomes a `parse_error`; that path feeds
  `parse_budget_exhausted` and parks the loop, turning a bookkeeping field into
  an outage — "the correction is a denial, never a parse error" below;
* the value is still NEVER acted on, so a real verb named here cannot become a
  second way to choose one — "counted, never executed" below;
* the tally is actually WRITTEN, because a required field that still produces no
  file has fixed nothing — "the tally is written" below.

Cheap tests where the claim is about a pure function (the parser, the policy
verdict, the schema string) and a real repository only where the claim is about
what the loop puts on disk — the split `CLAUDE.md` asks for.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from autoloop import orchestrator as orchestrator_module
from autoloop.contract import (
    ACTIVE_DECISIONS,
    CONTRACT_INSTRUCTIONS,
    NO_WANTED_DECISION,
    PROTOCOL_VERSION,
    RETIRED_DECISIONS,
    Decision,
    Directive,
    parse_response,
)
from autoloop.errors import ContractError
from autoloop.orchestrator import WantedDecisionTally, wanted_decisions_file
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import Phase
from autoloop.tasks import Task, TaskRegistry

# The wanted-verb machinery already has a home and a full orchestrator fixture
# there; borrowing it is the same sibling import `test_policy_denial_budget.py`
# and `test_rounds_and_restart.py` already make of `test_orchestrator.build`.
from test_recut import (
    block,
    build,
    denial_codes,
    implement_block,
    ready_task,
    records,
    stop_block,
)

DENIED = "wanted_decision_missing"


def payload(decision: str, **extra) -> dict:
    """A minimal well-formed reply for `decision`, carrying NO
    `wanted_decision` unless a caller adds one."""
    data: dict = {"version": PROTOCOL_VERSION, "decision": decision, "reason": "r"}
    if decision in ("implement", "revise", "recut", "split"):
        data["task_id"] = "t1"
    if decision == "revise":
        data["feedback"] = "fix it"
    if decision == "implement":
        data["decomposition"] = {
            "approach": "one commit",
            "files": ["docs/A.md"],
            "steps": ["write the file"],
        }
    if decision == "plan":
        data["tasks"] = [{"id": "t2", "title": "T", "description": "d"}]
    if decision == "split":
        data["tasks"] = [
            {"id": "t1-a", "title": "A", "description": "d"},
            {"id": "t1-b", "title": "B", "description": "d"},
        ]
    if decision in ("commit", "commit_and_push"):
        data["commit"] = {"message": "m", "paths": ["a.py"]}
    if decision in ("commit", "push", "commit_and_push"):
        data["reviewed"] = {
            "request_id": "alr-x-0001",
            "head_sha": "a" * 40,
            "report_sha256": "b" * 64,
        }
    data.update(extra)
    return data


def engine(**overrides) -> PolicyEngine:
    return PolicyEngine(PolicyConfig(implement_enabled=True, **overrides))


def registry() -> TaskRegistry:
    return TaskRegistry(
        [Task(id="t1", title="T", description="d", approved_paths=("docs/A.md",))]
    )


def directive(decision: Decision, **overrides) -> Directive:
    """A directive that would be authorized but for whatever `overrides` say —
    the plan is on board so a task decision is not refused for the OTHER
    optional-here-required-by-policy field."""
    from autoloop.contract import Decomposition

    fields: dict = {
        "decision": decision,
        "reason": "r",
        "commit_message": "m",
        "wanted_decision": NO_WANTED_DECISION,
    }
    if decision in (Decision.IMPLEMENT, Decision.REVISE):
        fields["task_id"] = "t1"
        fields["decomposition"] = Decomposition(
            approach="a", files=("docs/A.md",), steps=("s",)
        )
    fields.update(overrides)
    return Directive(**fields)


# ---------------------------------------------------------------------------
# the parser is unchanged — the wire compatibility half of the claim
# ---------------------------------------------------------------------------


def test_the_protocol_version_did_not_move():
    """Requiring the field in the PARSER would be a breaking wire change. It is
    required one layer up instead, so the version the reviewer is told to send
    is the version it has always sent."""
    assert PROTOCOL_VERSION == 3


@pytest.mark.parametrize(
    "decision", sorted(d.value for d in ACTIVE_DECISIONS | RETIRED_DECISIONS)
)
def test_a_reply_that_omits_the_field_still_parses(decision):
    """Every decision, including the retired one whose retirement depends on
    still parsing. `None`, not a raise: what the reviewer sends is well-formed,
    and whether it is AUTHORIZED is a different layer's question."""
    parsed = parse_response(block(payload(decision)))
    assert parsed.decision.value == decision
    assert parsed.wanted_decision is None


@pytest.mark.parametrize("blank", ["", " ", "   ", "\n", "\t \n"])
def test_a_blank_answer_parses_as_no_answer_rather_than_as_malformed(blank):
    """THE constraint that keeps this a bookkeeping field instead of an outage.
    A blank is the non-answer a model asked a new question is most likely to
    produce; raising here would spend `max_parse_retries` (2) and park the loop
    on `parse_budget_exhausted`, where reading it as absence routes it to the
    denial budget and a correction that explains itself."""
    assert parse_response(block(payload("stop", wanted_decision=blank))).wanted_decision is None


def test_a_non_string_answer_is_still_a_shape_error():
    """Absence and a WRONG TYPE are not the same thing: there is no answer to
    count either way, but `7` is a malformed reply rather than an unanswered
    question, and correcting a type is what the parse budget is for."""
    with pytest.raises(ContractError) as exc:
        parse_response(block(payload("stop", wanted_decision=7)))
    assert exc.value.code == "bad_type:wanted_decision"


@pytest.mark.parametrize("value", sorted(d.value for d in Decision) + ["rebase", "none"])
def test_an_answer_is_kept_verbatim_and_never_becomes_a_decision(value):
    """Still not validated against `Decision`, deliberately — a reviewer naming
    an existing verb is telling us the instructions are unclear, which is a
    signal to record rather than an error to raise — and still a plain `str`,
    which is what makes it structurally unable to select a branch."""
    parsed = parse_response(block(payload("stop", wanted_decision=value)))
    assert parsed.wanted_decision == value
    assert type(parsed.wanted_decision) is str


# ---------------------------------------------------------------------------
# the question changed, not only the requirement
# ---------------------------------------------------------------------------


def test_the_schema_asks_on_every_reply_and_offers_the_adequate_answer():
    """"when none above fits" is a condition that almost never holds, so the
    question was effectively never posed. It is `(required)` now — the same word
    `version`, `decision` and `reason` carry, which is how this schema spells
    "on every reply" — and it names the value that means the vocabulary was
    enough, without which a reviewer with nothing to report has no way to
    answer."""
    line = _schema_line()
    assert "(required)" in line
    assert "(optional)" not in line
    assert NO_WANTED_DECISION in line
    # The condition that made the old question unanswerable in practice is gone.
    assert "none above fits" not in CONTRACT_INSTRUCTIONS


def test_the_schema_still_says_the_answer_is_never_acted_on():
    """The reviewer has to know that naming a real verb here does NOT run it;
    otherwise the honest answer and the useful answer come apart."""
    line = _schema_line()
    assert "NEVER acted on" in line
    assert "executes `decision`" in line


def test_the_adequate_answer_is_spelled_the_same_in_the_prompt_and_in_the_code():
    """`NO_WANTED_DECISION` is the one spelling, read by the schema text, the
    denial and the loop's own directives. The prompt has to carry the literal
    (it is a plain string full of `{...}` shape examples), so the two are pinned
    to each other here instead — a second copy would agree today and disagree
    silently the first time one of them moved."""
    assert f"`{NO_WANTED_DECISION}`" in CONTRACT_INSTRUCTIONS
    assert NO_WANTED_DECISION in PolicyEngine(PolicyConfig())._check_wanted_decision(
        Directive(decision=Decision.STOP, reason="r")
    ).reason


def _schema_line() -> str:
    """The `wanted_decision` entry of the schema, from its key to the next one.

    Read out of the real text rather than restated, so a test asserting what the
    reviewer is told cannot pass on a sentence the reviewer never sees."""
    start = CONTRACT_INSTRUCTIONS.index("  wanted_decision ")
    end = CONTRACT_INSTRUCTIONS.index("NEVER put a literal line break", start)
    return CONTRACT_INSTRUCTIONS[start:end]


# ---------------------------------------------------------------------------
# policy denies a directive that does not answer — EVERY decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision", sorted(d.value for d in ACTIVE_DECISIONS))
def test_every_active_decision_without_an_answer_is_denied(decision):
    """"Every directive" means every one: an approval, a plan, a split and a
    `stop` are all replies, and a gate that let one shape through would leave
    the tally missing exactly the rounds that shape covers."""
    verdict = engine().authorize_directive(
        parse_response(block(payload(decision))), "feature/x", registry()
    )
    assert not verdict.allowed
    assert verdict.code == DENIED


@pytest.mark.parametrize("decision", sorted(d.value for d in ACTIVE_DECISIONS))
def test_the_same_directive_with_an_answer_is_never_denied_for_this(decision):
    """The correction has to be answerable on the same round, or it is a wall
    rather than a redirect. Whatever else the verdict is — allowed, phase-gated,
    push-disabled — it is not this denial once the question is answered."""
    verdict = engine().authorize_directive(
        parse_response(
            block(payload(decision, wanted_decision=NO_WANTED_DECISION))
        ),
        "feature/x",
        registry(),
    )
    assert verdict.code != DENIED


@pytest.mark.parametrize("value", ["none", "split", "defer", "PUSH", "  recut  "])
def test_any_non_empty_answer_passes_the_gate(value):
    """PRESENCE and nothing else. The gate never checks the value against
    `Decision` or against a fixed vocabulary — a reviewer whose missing verb has
    no name yet must still be able to name it, and refusing an unfamiliar answer
    would make the tally a record of what we already thought of."""
    verdict = engine().authorize_directive(
        directive(Decision.STOP, wanted_decision=value), "feature/x", registry()
    )
    assert verdict.allowed


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_answer_is_denied_exactly_like_an_absent_one(blank):
    """The parser reads a blank as absence, and this gate does NOT rely on that
    holding: it strips before testing, so a hand-built or future-parsed `" "` is
    still a non-answer here. A guard that quietly passes the one input it most
    needs to catch is the fail-open this test exists for."""
    verdict = engine().authorize_directive(
        directive(Decision.STOP, wanted_decision=blank), "feature/x", registry()
    )
    assert not verdict.allowed
    assert verdict.code == DENIED


@pytest.mark.parametrize("decision", sorted(d.value for d in RETIRED_DECISIONS))
def test_a_retired_decision_still_draws_its_own_denial(decision):
    """The retirement is unconditional and stays ahead of this gate. Telling a
    reviewer to re-send an `ask_user` with one more field would be a correction
    that cannot succeed — the decision is gone whatever else the reply
    carries."""
    verdict = engine().authorize_directive(
        parse_response(block(payload(decision))), "feature/x", registry()
    )
    assert not verdict.allowed
    assert verdict.code != DENIED


def test_the_denial_says_what_to_send_and_that_it_will_not_be_acted_on():
    """A denial is a corrective re-prompt: it has to name the field, name the
    answer for "nothing was missing", and say that a real verb written here is
    counted rather than executed — or a reviewer will read the requirement as a
    second way to choose a decision."""
    reason = engine().authorize_directive(
        parse_response(block(payload("stop"))), "feature/x", registry()
    ).reason
    assert "wanted_decision" in reason
    assert f"`{NO_WANTED_DECISION}`" in reason
    assert "NEVER acted on" in reason
    assert "no attempt was spent" in reason


def test_the_gate_is_not_configurable_off():
    """No policy flag admits an unanswered directive. `implement_enabled`,
    `allow_commit` and `allow_push` all change what a directive may DO; none of
    them changes whether it has to answer."""
    for config in (
        PolicyConfig(),
        PolicyConfig(implement_enabled=True, allow_commit=True, allow_push=True),
        PolicyConfig(implement_enabled=False, allow_commit=False, allow_push=False),
    ):
        verdict = PolicyEngine(config).authorize_directive(
            directive(Decision.STOP, wanted_decision=None), "feature/x", registry()
        )
        assert (verdict.allowed, verdict.code) == (False, DENIED)


def test_no_directive_the_loop_issues_to_itself_can_skip_the_gate():
    """`orchestrator.py` builds directives of its own (the self-issued revise
    that returns a refusal to the agent as feedback), and they pass through the
    SAME `authorize_directive`. They carry the answer rather than being exempted
    — an exemption would be a route to dispatch for a directive that answered
    nothing, and there is no reviewer behind those to ask.

    Checked structurally, over every `Directive(...)` constructed in that
    module, so a THIRD self-issued directive added later fails here instead of
    being denied in production at the moment it is first needed. Both spellings
    are matched — the bare name this module imports and a qualified
    `contract.Directive(...)` — because a guard that reads one of them is a
    guard the next author disables by writing the other."""
    tree = ast.parse(Path(orchestrator_module.__file__).read_text(encoding="utf-8"))
    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "Directive")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "Directive")
        )
    ]
    assert built, "no Directive construction found — has the module moved?"
    for node in built:
        keywords = {kw.arg for kw in node.keywords}
        assert "wanted_decision" in keywords, (
            f"Directive built at orchestrator.py:{node.lineno} answers no "
            "wanted verb and would be denied by policy"
        )


def test_the_tally_counts_reviewer_answers_and_nothing_the_loop_said_itself():
    """The anti-echo bound. `NO_WANTED_DECISION` is the honest answer for a
    directive the LOOP wrote, but counting it would put the loop's own words in
    the file an operator reads as evidence about REVIEWERS — a tally that grew
    on rounds no reviewer answered would be worse than the empty one it
    replaces.

    Structural because that is where the property lives: `_record_wanted_decision`
    is reached from `_step_executing` and from nowhere else, and a self-issued
    revise goes to `_dispatch` directly. A second call site added anywhere in
    the module fails here."""
    tree = ast.parse(Path(orchestrator_module.__file__).read_text(encoding="utf-8"))
    callers = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "_record_wanted_decision"
            for inner in ast.walk(node)
        )
    ]
    assert callers == ["_step_executing"]


# ---------------------------------------------------------------------------
# the tally is written — including for the "nothing was missing" answer
# ---------------------------------------------------------------------------


def test_the_adequate_answer_is_counted_like_any_other(tmp_path):
    """Special-cased NOWHERE. `none x412, split x9` is evidence; a file holding
    only the exotic answers would be the same zero tally in a smaller font."""
    tally = WantedDecisionTally(wanted_decisions_file(tmp_path / "st"))
    tally.record(NO_WANTED_DECISION)
    tally.record("split")
    counts, _ = tally.record(NO_WANTED_DECISION)
    assert counts == {NO_WANTED_DECISION: 2, "split": 1}


def test_a_real_round_writes_the_tally_file(tmp_path):
    """THE measured failure this task exists for: the file an operator is
    supposed to read had never been created, because nothing had ever written to
    it. One ordinary round, driven end to end, now leaves it on disk."""
    wiring = build(
        tmp_path, responses=[implement_block("t1"), stop_block()], tasks=[ready_task("t1")]
    )
    wiring.orch.run()

    path = wanted_decisions_file(wiring.config.state_dir)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))[NO_WANTED_DECISION] >= 1
    assert records(wiring, "wanted_decision")[0]["wanted"] == NO_WANTED_DECISION


def test_a_named_verb_is_counted_and_the_loop_still_does_what_decision_says(tmp_path):
    """The bound the field is designed around, exercised through the REAL
    `_step_executing` path rather than a hand-built dispatch: a live candidate
    exists, the reviewer sends `stop` while naming `recut` as the verb it
    wanted, and afterwards the candidate is untouched, the loop is stopped, and
    the only thing the wanted verb produced is a number in a file."""
    wiring = build(
        tmp_path,
        responses=[implement_block("t1"), stop_block(wanted="recut")],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()
    execution = wiring.execution_store.load("t1")

    assert wiring.orch.state.phase == Phase.STOPPED.value
    assert execution is not None and execution.candidate_sha
    assert (wiring.tmp_path / "workers" / "t1").is_dir()
    assert not (wiring.tmp_path / "quarantine").exists()
    assert records(wiring, "task_recut") == []
    counts = json.loads(
        wanted_decisions_file(wiring.config.state_dir).read_text(encoding="utf-8")
    )
    assert counts["recut"] == 1


# ---------------------------------------------------------------------------
# the correction is a denial, never a parse error
# ---------------------------------------------------------------------------


def unanswered_stop() -> str:
    """A `stop` that is perfectly well-formed and simply does not answer the
    question — built with the raw `block`, which (unlike the other suites'
    helpers) supplies no default."""
    return block({"version": 3, "decision": "stop", "reason": "done"})


def test_an_unanswered_directive_is_re_prompted_and_nothing_is_executed(tmp_path):
    """The denial is a REDIRECT: the loop goes back to `ready` with the
    correction in the outbox, no executor round is started, no attempt is spent,
    and the reviewer's next reply lands normally."""
    wiring = build(
        tmp_path,
        responses=[unanswered_stop(), implement_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()

    assert denial_codes(wiring)[0] == DENIED
    assert records(wiring, "parse_error") == []
    assert wiring.orch.state.phase == Phase.STOPPED.value
    # The round after the correction did what it said, so the denial cost one
    # exchange rather than the run.
    assert [task.id for _, task in wiring.executor.calls] == ["t1"]


def test_the_correction_spends_the_denial_budget_and_never_the_parse_budget(tmp_path):
    """The whole reason this gate is in policy and not in the parser. A reviewer
    that never answers ends the run on `policy_denial_budget_exhausted` — a
    bounded, self-explaining terminal — where a `parse_error` would have spent
    `max_parse_retries` (2) and parked on `parse_budget_exhausted`, holding an
    autonomous session open for a human. Not one `parse_error` is logged."""
    wiring = build(
        tmp_path,
        responses=[unanswered_stop() for _ in range(8)],
        tasks=[ready_task("t1")],
        policy=PolicyConfig(implement_enabled=True, max_policy_denials=3),
    )
    wiring.orch.run()

    assert records(wiring, "parse_error") == []
    assert set(denial_codes(wiring)) == {DENIED}
    assert wiring.orch.state.phase == Phase.STOPPED.value
    assert wiring.orch.state.stop_kind == "fault"
    assert wiring.executor.calls == []
    # And the run ended on the budget rather than on the fixture running out of
    # scripted replies, which is what makes "capped" mean anything.
    assert len(denial_codes(wiring)) == 4


def test_an_unanswered_directive_writes_nothing_to_the_tally(tmp_path):
    """Absence is not an occurrence. Counting the empty answer would put a
    phantom key in the operator's only evidence file — and the point of the
    change is that a tally with nothing in it can no longer be confused with a
    question nobody asked."""
    wiring = build(
        tmp_path,
        responses=[unanswered_stop(), stop_block()],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()

    counts = json.loads(
        wanted_decisions_file(wiring.config.state_dir).read_text(encoding="utf-8")
    )
    assert counts == {NO_WANTED_DECISION: 1}
