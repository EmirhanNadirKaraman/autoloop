"""The `codex_cli` adapter hands the contract a parseable verdict.

**The defect, reproduced 2026-08-17.** `codex/conversation.py` did
`reply = result.stdout.strip()` and gave the WHOLE of it to
`contract.parse_response`. `codex exec` stdout is a rendered transcript — a
separator rule, a `user` marker with the echoed prompt under it, hook lines, a
`codex` marker with the answer under it, a token counter — and it closes by
printing the answer A SECOND TIME. The real parser answers that with
`invalid_json: the reply is not exactly one JSON value`, so the provider could
not return a verdict at all.

**The fixture here is that stdout, not a clean one.** A clean-output fixture
proves nothing: clean output is not what this CLI produces. `decorate` builds
the exact shape that was captured; the first test below pins it against a
verbatim copy so it cannot drift into a tidier one, and the second asserts the
defect is real by feeding the parser the whole thing.

**Nothing here weakens the contract, and these tests say so from both ends.**
The parser is untouched: it still refuses prose-plus-a-bare-object and still
refuses a second object. What changed is the INPUT it is given. So a prose reply
is isolated and then refused by the contract (the corrective re-prompt is the
right answer to a reviewer that answered in prose), while stdout from which no
message could be isolated at all is a FAILED invocation — never a defaulted
decision, and never a verdict chosen from two by position.

**The echo is the case that matters most.** The prompt comes back under the
`user` marker and the prompt carries the response contract; in the shape that
was reproduced it also carries an example directive. Text the loop SENT, read
back as the reviewer's answer, would be an approval nobody gave.
"""

import json

import pytest

from autoloop.codex.conversation import CodexConversation, CodexResult
from autoloop.codex.reply import (
    FROM_NOTHING,
    FROM_SEGMENT,
    FROM_WHOLE_STDOUT,
    codex_segments,
    isolate_reply,
    role_marker_count,
)
from autoloop.contract import NO_WANTED_DECISION, Decision, parse_response
from autoloop.conversation import SubmitResult
from autoloop.errors import ContractError, ResponseTimeoutError

RID = "alr-codex-0017"
PROMPT = f"[autoloop request {RID} | iteration 1]\n\nreview this candidate"

#: The object from the reproduction, verbatim. Three keys, and therefore NOT a
#: complete `push` — `push` is in `contract.REVIEWED_DECISIONS`, so the parser
#: demands a `reviewed` stamp. That is a property of the contract and not of
#: this task, which is why isolation is asserted on this object and PARSING is
#: asserted on the complete one below, in the same decoration.
SMOKE_VERDICT = '{"version": 3, "decision": "push", "reason": "smoke test"}'

#: A complete push directive: what a reviewer actually sends, in the stdout
#: shape this CLI actually prints.
PUSH_DIRECTIVE = {
    "version": 3,
    "decision": "push",
    "reason": "the reviewed commit is ready to publish",
    "reviewed": {
        "request_id": RID,
        "head_sha": "9f1c2d3e4b5a60718293a4b5c6d7e8f901234567",
        "report_sha256": "a" * 64,
    },
    "wanted_decision": NO_WANTED_DECISION,
}

#: A DIFFERENT complete directive, for the echo case. If the adapter ever read
#: the `user` segment as the answer, this is the verb it would execute.
ECHOED_DIRECTIVE = {
    "version": 3,
    "decision": "commit_and_push",
    "reason": "the echoed example the prompt asked the reviewer to copy",
    "commit": {"message": "example", "paths": ["autoloop/codex/reply.py"]},
    "reviewed": {
        "request_id": "alr-codex-0001",
        "head_sha": "0" * 40,
        "report_sha256": "b" * 64,
    },
    "wanted_decision": NO_WANTED_DECISION,
}


def decorate(verdict, *, echoed="{...}"):
    """The exact stdout `codex exec` produced on this machine, around `verdict`.

    Role markers, four hook lines before the answer and two after, the token
    counter, and the answer repeated as the run's closing summary. `echoed` is
    what the `user` segment carries — the prompt, coming back.
    """
    return (
        "\n".join(
            [
                "--------",
                "user",
                f"Reply with exactly this JSON and nothing else: {echoed}",
                "hook: SessionStart",
                "hook: SessionStart Completed",
                "hook: UserPromptSubmit",
                "hook: UserPromptSubmit Completed",
                "codex",
                verdict,
                "hook: Stop",
                "hook: Stop Completed",
                "tokens used",
                "6,080",
                verdict,
            ]
        )
        + "\n"
    )


REPORTED_STDOUT = decorate(SMOKE_VERDICT)
PUSH_STDOUT = decorate(json.dumps(PUSH_DIRECTIVE))


class FakeRunner:
    """Scripted `codex exec`. No binary is involved anywhere in this file."""

    def __init__(self, results):
        self.results = list(results)
        self.prompts: list[str] = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def result(stdout="", stderr="", returncode=0):
    return CodexResult(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        duration_seconds=0.1,
        command=("codex", "exec"),
    )


def adapter(stdout, **kwargs):
    """A `CodexConversation` over one scripted invocation, plus its log."""
    logged: list[tuple[str, dict]] = []
    codex = CodexConversation(
        FakeRunner([result(stdout=stdout, **kwargs)]),
        log=lambda event, data: logged.append((event, data)),
    )
    return codex, logged


def rows(logged, event):
    return [data for name, data in logged if name == event]


# ---- the fixture is the output that was actually captured --------------------


def test_the_fixture_is_the_stdout_that_was_reported():
    """`decorate` is a helper, and a helper that quietly tidies its subject is a
    test that passes while covering nothing. Pinned against a verbatim copy of
    what was captured, byte for byte."""
    assert REPORTED_STDOUT == (
        "--------\n"
        "user\n"
        "Reply with exactly this JSON and nothing else: {...}\n"
        "hook: SessionStart\n"
        "hook: SessionStart Completed\n"
        "hook: UserPromptSubmit\n"
        "hook: UserPromptSubmit Completed\n"
        "codex\n"
        '{"version": 3, "decision": "push", "reason": "smoke test"}\n'
        "hook: Stop\n"
        "hook: Stop Completed\n"
        "tokens used\n"
        "6,080\n"
        '{"version": 3, "decision": "push", "reason": "smoke test"}\n'
    )
    # The duplicate is really there — it is the whole reason the old code could
    # not work, and a fixture carrying it once would prove nothing.
    assert REPORTED_STDOUT.count(SMOKE_VERDICT) == 2


def test_the_reported_stdout_still_defeats_the_parser():
    """The defect, asserted rather than described. `result.stdout.strip()` fed
    straight to the contract is exactly this call."""
    with pytest.raises(ContractError) as exc:
        parse_response(REPORTED_STDOUT.strip())
    assert exc.value.code == "invalid_json"


# ---- the mechanical acceptance ----------------------------------------------


def test_the_verdict_is_isolated_from_the_reported_stdout():
    """The verbatim fixture, and the object it carries, isolated exactly."""
    isolated = isolate_reply(REPORTED_STDOUT)
    assert isolated.text == SMOKE_VERDICT
    assert isolated.source == FROM_SEGMENT
    assert isolated.segments == 1
    # Isolated ONCE, though stdout carries it twice.
    assert isolated.text.count(SMOKE_VERDICT) == 1


def test_the_decorated_stdout_parses_to_one_push_directive():
    """The mechanical acceptance: the same decoration — hook lines, role
    markers, trailing token count, duplicated verdict — around a complete push
    directive parses to ONE directive with decision `push`."""
    directive = parse_response(isolate_reply(PUSH_STDOUT).text)
    assert directive.decision is Decision.PUSH
    # The stamp survives intact: an approval is only worth anything if the
    # thing it authorizes came through untouched.
    assert directive.reviewed.request_id == RID
    assert directive.reviewed.head_sha == PUSH_DIRECTIVE["reviewed"]["head_sha"]
    assert directive.reviewed.report_sha256 == PUSH_DIRECTIVE["reviewed"]["report_sha256"]


def test_a_fenced_reply_is_isolated_out_of_two_fenced_blocks():
    """The CANONICAL shape, which is what production will actually see:
    `CONTRACT_INSTRUCTIONS` asks for one fenced ```json block, so the closing
    summary makes stdout carry TWO of them. The contract refuses that with a
    different code than the bare-object shape does — `multiple_json_blocks`
    rather than `invalid_json` — and the same boundary fixes both."""
    stdout = decorate(f"```json\n{json.dumps(PUSH_DIRECTIVE)}\n```")

    with pytest.raises(ContractError) as exc:
        parse_response(stdout.strip())
    assert exc.value.code == "multiple_json_blocks"

    assert parse_response(isolate_reply(stdout).text).decision is Decision.PUSH


def test_the_echoed_prompt_is_never_read_as_the_verdict():
    """The ECHO, which is the failure this boundary exists to make impossible:
    the `user` segment carries a COMPLETE and DIFFERENT directive, because the
    prompt shows the reviewer the shape it wants. The verb that comes back must
    be the reviewer's, not the loop's own text returning."""
    stdout = decorate(json.dumps(PUSH_DIRECTIVE), echoed=json.dumps(ECHOED_DIRECTIVE))
    # The echoed directive really is complete and really would parse on its own.
    assert parse_response(json.dumps(ECHOED_DIRECTIVE)).decision is Decision.COMMIT_AND_PUSH

    directive = parse_response(isolate_reply(stdout).text)
    assert directive.decision is Decision.PUSH
    assert directive.commit_message is None


def test_the_trailing_duplicate_is_excluded_by_the_token_counter_not_by_counting():
    """Structural, not "de-duplicate identical objects". The closing copy sits
    after `tokens used`, which ends the message, so it is never inside one — and
    a copy that DIFFERED would be excluded by the same rule, which is the case
    that matters the day a hook prints something unexpected."""
    other = '{"version": 3, "decision": "stop", "reason": "a different tail"}'
    stdout = REPORTED_STDOUT.replace(f"6,080\n{SMOKE_VERDICT}", f"6,080\n{other}")
    assert other in stdout and stdout.count(SMOKE_VERDICT) == 1

    isolated = isolate_reply(stdout)
    assert isolated.text == SMOKE_VERDICT
    assert other not in isolated.text


# ---- no verdict is a failed invocation, never a default ---------------------


def test_a_role_marker_with_no_message_isolates_nothing():
    """A marker and no message is not an empty reply to hand on — it is a
    missing one, and the two must not collapse into the pass-through below."""
    isolated = isolate_reply("--------\ncodex\nhook: Stop\ntokens used\n5\n")
    assert isolated.text == ""
    assert isolated.source == FROM_NOTHING
    assert "no message" in isolated.note


def test_two_codex_messages_are_refused_rather_than_chosen_between():
    """The contract's own rule, applied one layer up: "guess which one they
    meant" is not acceptable for a directive that can authorize a push. Two
    identical objects today are two different objects tomorrow."""
    stdout = (
        "codex\n"
        f"{SMOKE_VERDICT}\n"
        "hook: Stop\n"
        "codex\n"
        '{"version": 3, "decision": "stop", "reason": "second message"}\n'
        "tokens used\n"
        "12\n"
    )
    isolated = isolate_reply(stdout)
    assert isolated.text == ""
    assert isolated.segments == 2
    assert "position" in isolated.note


def test_two_identical_messages_are_refused_too():
    """Agreement between two copies is not evidence, it is a coincidence that
    holds until it does not — and reading it as agreement is the rule that would
    then pick silently."""
    stdout = f"codex\n{SMOKE_VERDICT}\nhook: Stop\ncodex\n{SMOKE_VERDICT}\ntokens used\n9\n"
    assert isolate_reply(stdout).text == ""


def test_empty_stdout_isolates_nothing_and_keeps_its_wording():
    """The pre-existing note an operator greps for, unchanged."""
    for stdout in ("", "   \n\n"):
        isolated = isolate_reply(stdout)
        assert isolated.text == ""
        assert "no reply on stdout" in isolated.note


# ---- the contract still judges the reply, and this module never does ---------


def test_a_prose_reply_is_isolated_and_left_for_the_contract_to_refuse():
    """The boundary's limit, stated. Isolation does not mean validation: a
    reviewer that answers in prose must draw the contract's corrective
    re-prompt, not a silent re-invocation of the CLI."""
    stdout = decorate("I am not able to approve this candidate.")
    isolated = isolate_reply(stdout)
    assert isolated.text == "I am not able to approve this candidate."

    with pytest.raises(ContractError) as exc:
        parse_response(isolated.text)
    assert exc.value.code == "no_json_block"


def test_a_second_object_inside_one_message_is_still_the_contracts_refusal():
    """Nothing here trims a message down to something that parses. Two objects
    printed as ONE message reach the contract as two, and are refused there."""
    stdout = decorate(f"{SMOKE_VERDICT}\n{SMOKE_VERDICT}")
    isolated = isolate_reply(stdout)
    assert isolated.text.count(SMOKE_VERDICT) == 2

    with pytest.raises(ContractError):
        parse_response(isolated.text)


def test_stdout_that_is_only_furniture_is_not_passed_through_as_a_reply():
    """The fail-open shape the pass-through could have had. Hooks and a token
    count with no role marker is decoration this build did not label — handing
    it on gets it refused by the contract, and a parse refusal is the expensive
    one: it spends the two-round parse budget and parks the loop loop_fatal,
    where a rejected invocation is bounded at one resend."""
    isolated = isolate_reply("hook: SessionStart\nhook: Stop Completed\ntokens used\n6,080\n")
    assert isolated.text == ""
    assert isolated.source == FROM_NOTHING
    assert "furniture" in isolated.note


def test_undecorated_stdout_is_passed_through_unchanged():
    """A build (or a fake) whose stdout IS the reply keeps working byte for
    byte, which is what every caller got before this boundary existed."""
    raw = 'Reasoning...\n```json\n{"version": 3, "decision": "stop"}\n```'
    isolated = isolate_reply(raw)
    assert isolated.text == raw
    assert isolated.source == FROM_WHOLE_STDOUT
    assert isolated.segments == 0


# ---- what counts as furniture, and what does not ----------------------------


def test_a_role_marker_is_a_whole_line_never_a_prefix():
    """`codex exec is running` is prose. Opening a message inside a diagnostic
    would put the diagnostic in front of the contract."""
    assert role_marker_count("codex exec is running\ncodex CLI 0.9.1\n") == 0
    assert role_marker_count("codex\n  codex  \nuser\n") == 2


def test_a_hook_line_inside_a_json_value_cannot_truncate_the_message():
    """The furniture rules are anchored at the start of the stripped line, and a
    JSON line starts with `{`, `"`, `[` or `}` — so a directive that QUOTES a
    hook line survives whole."""
    directive = dict(PUSH_DIRECTIVE, reason="the run logged hook: Stop as expected")
    pretty = json.dumps(directive, indent=2)
    assert "hook: Stop" in pretty

    parsed = parse_response(isolate_reply(decorate(pretty)).text)
    assert parsed.decision is Decision.PUSH
    assert parsed.reason == "the run logged hook: Stop as expected"


def test_a_blank_line_does_not_end_a_message():
    """A blank line is not furniture. Ending a message at one would truncate a
    pretty-printed directive at its first empty line — and the strip at the end
    removes the trailing blanks that a real message picks up anyway."""
    body = f"```json\n\n{SMOKE_VERDICT}\n\n```"
    assert isolate_reply(decorate(body)).text == body


def test_a_separator_rule_and_a_following_turn_both_end_a_message():
    """The two shapes that close a message with no hook line between."""
    assert codex_segments(f"codex\n{SMOKE_VERDICT}\n--------\ntrailing\n") == (
        SMOKE_VERDICT,
    )
    assert codex_segments(f"codex\n{SMOKE_VERDICT}\nuser\na second turn\n") == (
        SMOKE_VERDICT,
    )


def test_the_token_counter_closes_a_message_on_either_of_its_two_shapes():
    """The captured output puts the count on the next line; a build that writes
    `tokens used: 6,080` on one must close the message just the same, or the
    closing duplicate lands inside it."""
    for counter in ("tokens used\n6,080", "tokens used: 6,080"):
        stdout = f"codex\n{SMOKE_VERDICT}\n{counter}\n{SMOKE_VERDICT}\n"
        assert isolate_reply(stdout).text == SMOKE_VERDICT, counter


def test_a_message_that_runs_to_the_end_of_stdout_is_still_isolated():
    """No trailing furniture at all — a build that prints no token counter must
    not lose the reply for want of a terminator."""
    assert isolate_reply(f"--------\nuser\nprompt\ncodex\n{SMOKE_VERDICT}\n").text == (
        SMOKE_VERDICT
    )


# ---- the adapter seam -------------------------------------------------------


def test_the_adapter_hands_its_caller_the_isolated_verdict():
    """The whole task at the seam: `await_response` returns text the contract
    parses, from the stdout this CLI actually produces."""
    codex, _ = adapter(PUSH_STDOUT)
    assert codex.submit(RID, PROMPT) is SubmitResult.CONFIRMED
    assert parse_response(codex.await_response(RID)).decision is Decision.PUSH


def test_stdout_with_no_isolatable_verdict_fails_the_invocation():
    """Never a defaulted decision. The invocation is REJECTED — retryable, and
    bounded by `_step_submission_rejected` at one resend and then a park — and
    nothing is stashed for `await_response` to hand back."""
    stdout = f"codex\n{SMOKE_VERDICT}\nhook: Stop\ncodex\n{SMOKE_VERDICT}\ntokens used\n9\n"
    codex, logged = adapter(stdout)

    assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED
    assert codex.has_request(RID) is False
    assert codex.reconcile(RID) is False
    with pytest.raises(ResponseTimeoutError):
        codex.await_response(RID)

    failures = rows(logged, "codex_invocation_failed")
    assert len(failures) == 1
    assert "position" in failures[0]["note"]
    assert failures[0]["request_id"] == RID
    # And no isolation was claimed for an invocation that isolated nothing.
    assert not rows(logged, "codex_reply_isolated")


def test_a_role_marker_with_no_message_fails_the_invocation_too():
    codex, logged = adapter("--------\ncodex\nhook: Stop\ntokens used\n5\n")
    assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED
    assert "no message" in rows(logged, "codex_invocation_failed")[0]["note"]


def test_the_isolation_leaves_a_visible_counts_only_record():
    """A rule that silently discards output cannot be told apart from a rule
    that never fired — the reason `quota.py` records `suppressed_patterns`. The
    record is COUNTS ONLY: stdout carries the echoed prompt, which is the whole
    review packet."""
    codex, logged = adapter(PUSH_STDOUT)
    codex.submit(RID, PROMPT)

    isolations = rows(logged, "codex_reply_isolated")
    assert len(isolations) == 1
    record = isolations[0]
    assert record["segments"] == 1
    assert record["stdout_chars"] == len(PUSH_STDOUT)
    assert record["reply_chars"] < record["stdout_chars"]
    assert record["request_id"] == RID
    # Nothing but the request id is text, so no packet content can ride out on
    # this record however large the prompt or the reply was.
    assert set(record) == {"request_id", "segments", "stdout_chars", "reply_chars"}
    assert not any(isinstance(v, str) for k, v in record.items() if k != "request_id")


def test_undecorated_stdout_claims_no_isolation():
    """The pass-through is not dressed up as a boundary that fired."""
    raw = 'Reasoning...\n```json\n{"version": 3, "decision": "stop", "reason": "x"}\n```'
    codex, logged = adapter(raw)
    assert codex.submit(RID, PROMPT) is SubmitResult.CONFIRMED
    assert codex.await_response(RID) == raw
    assert not rows(logged, "codex_reply_isolated")


def test_a_failed_exit_never_reaches_isolation_at_all():
    """Ordering, pinned: a non-zero exit is classified and recorded as it always
    was, and stdout that happens to carry a verdict does not rescue it."""
    codex, logged = adapter(PUSH_STDOUT, returncode=1, stderr="Error: boom")
    assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED
    assert not rows(logged, "codex_reply_isolated")
    assert rows(logged, "codex_invocation_failed")[0]["returncode"] == 1
