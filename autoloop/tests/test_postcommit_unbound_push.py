"""postcommit-01: a refused `push` must leave the reviewer able to produce an
approval this loop accepts — or must stop describing one.

THE INCIDENT (notify-01, 2026-08-31 20:41-20:55 UTC). The reviewer answered with
a bare `push`; `_dispatch` refused it as `legacy_git_path_retired`; the reviewer
rephrased and resent it 24 different ways across 71 consecutive denials, one of
them naming "the required integrity stamp" without producing one. The commit it
was arguing about (92f96ac) had merged 32 minutes earlier. At 22:35 the same
standoff on policy-01 reached the denial budget that task had just made honest
and STOPPED the loop (`blk-(loop)-055`). The bound worked; the cause did not.

THE CAUSE IS A FIXED POINT IN THE LOOP'S OWN RE-PROMPT, not in the reviewer.
`_handle_policy_denial` queues `policy_denied_payload`, a `failure_recovery`
render that carries none of a candidate's four identifiers — so
`_current_pending_postcommit` binds the NEXT request to nothing either, and the
same approval, about the same work, lands back on the same branch. Nothing in
the reply had to change for that to repeat forever.

And the refusal's own text asked for the one thing that state cannot produce:
"the only valid approval is `push` with the `reviewed` stamp answering a
postcommit ... review packet". After publication `_dispatch_task_push` clears
`state.task_execution` (orchestrator.py, "Clear `state.task_execution` now that
the candidate is actually published") and calls
`_forget_sent_postcommits_for_task`, so no packet is outstanding and no such
stamp exists anywhere. The reviewer was told what a valid approval looks like
while none could exist — case (c) of the task's own question.

THE TWO HALVES ARE NOT THE SAME KIND OF FIX, and saying so is the point:

  * section 1 — a `push` this loop CAN still answer re-presents the candidate,
    so the very next reply, the reviewer's identical behaviour, PUBLISHES. This
    half is mechanical: four consecutive denials are unreachable here because
    the second round has a bindable request in front of it.
  * section 2 — a `push` with nothing to approve is still refused, and FOUR OF
    THEM STILL STOP THE RUN. That is deliberate and is what policy-01 is for:
    there is genuinely nothing to approve, and stopping is the correct end. What
    changed is that the refusal no longer DESCRIBES an approval that cannot
    exist, so a reviewer reading it is not being asked to produce the artifact
    those 24 rephrasings were trying to produce. These tests assert the text,
    because the text is the whole of the change on this arm.

Neither relaxes anything: a bare push is still refused, still charged against
`max_policy_denials`, and every check in `_dispatch_task_push` still runs
(section 3).

Driven through `_step_ready` / `_step_executing` against real git, never by
calling `_handle_policy_denial` or `_dispatch` directly — policy-01's module
docstring says why in one line: "a test that calls it directly is exactly the
test that passed all through the incident".
"""

from __future__ import annotations

import json

from autoloop.errors import DiffTooLargeError
from autoloop.policy import PolicyConfig
from autoloop.state import Phase

# Sibling test modules, importable because pytest's prepend import mode puts this
# directory on `sys.path` — the same borrowing `test_policy_denial_budget.py`
# already does for `build`.
from test_policy_denial_budget import DENIALS_TO_EXHAUST
from test_postcommit_binding_carry import (
    WritingExecutor,
    _packet_then_unrelated_round,
    block,
    deliver,
    push_naming,
    send_packet,
    transcript_of,
    with_origin,
    worktree_git_for,
)

LEGACY = "legacy_git_path_retired"

#: The clause that ASKS for an approval. Present where one can exist, absent
#: where none can.
APPROVAL_SHAPE = "the only valid approval is `push`"

#: The clause every branch of the refusal keeps byte-identical — what an
#: operator greps for, and what a budget-exhausted stop quotes back.
LEGACY_REASON = "direct commit/push is no longer supported"

#: The first line of `prompts.TEMPLATES["failure_recovery"]`, which is what
#: EVERY corrective re-prompt renders — read from the template rather than from
#: whatever a fixture's candidate happens not to contain, so "the correction was
#: replaced" is asserted against the correction itself.
CORRECTION_HEADER = "The previous step failed and nothing further was executed."

#: `_packet_then_unrelated_round` leaves the loop on `main`, which is protected.
#: An unbound `push` there is refused by `authorize_directive` ABOVE the
#: dispatch, which is a different denial from the one under test — the same
#: reason `test_postcommit_binding_carry.py`'s published-packet test gives for
#: the same flag. Production's loop branch is not protected.
UNPROTECTED = PolicyConfig(implement_enabled=True, allow_protected_push=True)


def events(config, entry_type: str) -> list[dict]:
    """The `data` payload of every transcript entry of one type."""
    path = config.transcript_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == entry_type:
            out.append(entry.get("data") or {})
    return out


def denial_codes(config) -> list[str]:
    return [entry.get("code") for entry in events(config, "policy_denied")]


def moved_past_the_packet(tmp_path, policy=UNPROTECTED):
    """A live, unpublished candidate on record with the conversation moved past
    the packet that presented it — the state an unbound `push` arrives in."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    return _packet_then_unrelated_round(tmp_path, executor, policy=policy)


# =============================================================================
# 1. THE CLAIM — the reviewer's own behaviour now reaches an approval
# =============================================================================


def test_a_bare_push_re_presents_the_candidate_and_the_next_one_publishes(tmp_path):
    """THE regression, end to end. The reviewer sends the incident's reply — a
    `push` stamping the request it answers, which presented no candidate — and
    sends the SAME reply again. Before this change both were refused
    identically and a third and fourth would have exhausted the budget; now the
    first re-presents the candidate and the second publishes it.

    Note what is NOT scripted here: no correction, no re-stamping, no change of
    verb. The reviewer behaves exactly as it did for 71 denials."""
    (orch, repo_root, worktrees, execution_store, task, config, _packet, later) = (
        moved_past_the_packet(tmp_path)
    )
    execution = execution_store.load(task.id)
    with_origin(tmp_path, repo_root)

    deliver(orch, later, push_naming(later))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value, "a re-prompt, not a park"
    assert denial_codes(config) == [LEGACY], "the push was refused, exactly once"
    assert orch.state.policy_denials == 1, "and charged"
    represented = events(config, "postcommit_represented")
    assert [entry["task_id"] for entry in represented] == [task.id]
    assert represented[0]["candidate_sha"] == execution.candidate_sha

    again = send_packet(orch)
    assert again.postcommit is not None, "the next request BINDS — the fixed point is broken"
    assert again.postcommit.candidate_sha == execution.candidate_sha

    deliver(orch, again, push_naming(again))
    orch._step_executing()

    wt_git = worktree_git_for(worktrees, task.id)
    dest_ref = f"refs/heads/{execution.task_branch}"
    assert wt_git.remote_ref_sha("origin", dest_ref) == execution.candidate_sha
    assert "task_pushed" in transcript_of(config)
    assert denial_codes(config) == [LEGACY], "no second refusal was needed"
    assert len(denial_codes(config)) < DENIALS_TO_EXHAUST
    assert orch.state.policy_denials == 0, "the streak ended on the acted-on push"


def test_the_re_presentation_is_the_whole_next_request_and_says_why(tmp_path):
    """The packet displaces the correction rather than being appended to it:
    `policy_denied_payload` carries none of the candidate's identifiers, and a
    request that is half a correction would still have to bind on the packet
    half. The refusal itself is not lost — it is the preamble."""
    (orch, _repo, _wt, execution_store, task, config, _packet, later) = (
        moved_past_the_packet(tmp_path)
    )
    execution = execution_store.load(task.id)

    deliver(orch, later, push_naming(later))
    orch._step_executing()

    outbox = orch.state.outbox or ""
    assert "RE-PRESENTED" in outbox
    assert task.id in outbox
    assert execution.candidate_sha in outbox
    assert execution.task_branch in outbox
    assert execution.task_base_sha in outbox
    assert CORRECTION_HEADER not in outbox, "the correction was replaced, not wrapped"
    # The denial is still what the transcript records, and its reason still
    # tells the reviewer what happened to its reply.
    reason = events(config, "policy_denied")[0]["reason"]
    assert LEGACY_REASON in reason
    assert "RE-PRESENTED" in reason


def test_the_re_presented_packet_is_not_a_new_review_round(tmp_path):
    """Re-presenting is the loop repairing its own request, not the task
    earning another review: the candidate, its tree and its report are the ones
    the reviewer was already shown. A round charged here would spend a budget
    (`max_review_rounds`) on the loop's own repair."""
    (orch, _repo, _wt, execution_store, task, _config, _packet, later) = (
        moved_past_the_packet(tmp_path)
    )
    before = execution_store.load(task.id)

    deliver(orch, later, push_naming(later))
    orch._step_executing()

    after = execution_store.load(task.id)
    assert after.review_round == before.review_round
    assert after.candidate_sha == before.candidate_sha
    assert after.published_sha == before.published_sha == ""


# =============================================================================
# 2. NOTHING TO APPROVE — the loop stops asking for an approval that cannot
#    exist. The notify-01 state: the work has already been published.
# =============================================================================


def published_then_bare_push(tmp_path):
    """Publish the candidate the way the loop does, then answer the report with
    a bare `push` — notify-01's exact shape, where the commit being argued about
    was already on origin."""
    (orch, repo_root, worktrees, execution_store, task, config, _packet, later) = (
        moved_past_the_packet(tmp_path)
    )
    execution = execution_store.load(task.id)
    with_origin(tmp_path, repo_root)

    # Round 1 re-presents; round 2 publishes. From here on there is genuinely
    # nothing left to approve.
    deliver(orch, later, push_naming(later))
    orch._step_executing()
    again = send_packet(orch)
    deliver(orch, again, push_naming(again))
    orch._step_executing()
    assert "task_pushed" in transcript_of(config)
    assert orch.state.task_execution is None

    report = send_packet(orch)
    assert report.postcommit is None
    deliver(orch, report, push_naming(report))
    orch._step_executing()
    return orch, worktrees, execution_store, task, config, execution


def test_a_push_with_nothing_to_approve_is_not_asked_for_a_stamp(tmp_path):
    """THE other half of the claim, and the smaller one. The refusal must not
    describe an approval shape while no packet exists to stamp — that
    description is what 24 rephrasings were trying to satisfy.

    A reviewer that ignores this and pushes four more times still exhausts the
    budget and still stops the run; nothing here makes that unreachable, and
    nothing should. There is no candidate, so there is no packet to send and no
    approval to reach — a stop is the correct end of that conversation."""
    orch, _worktrees, _store, _task, config, _execution = published_then_bare_push(
        tmp_path
    )

    outbox = orch.state.outbox or ""
    assert LEGACY_REASON in outbox, "the refusal is unchanged where it matters"
    assert APPROVAL_SHAPE not in outbox, "and asks for nothing that cannot exist"
    assert "NOTHING IS AWAITING PUBLICATION HERE" in outbox
    assert "`stop`" in outbox, "a move the reviewer can actually make is named"
    assert denial_codes(config)[-1] == LEGACY, "still refused, and still charged"


def test_nothing_is_re_presented_or_re_published_when_the_work_has_shipped(tmp_path):
    """The refusal in that state must not resurrect the candidate either. A
    re-presented published commit invites the second `push`
    `_forget_sent_postcommits_for_task` exists to refuse."""
    orch, worktrees, execution_store, task, config, execution = (
        published_then_bare_push(tmp_path)
    )

    # Exactly one, from round 1 — BEFORE publication, when there really was a
    # candidate to show. The bare push after publication added none.
    assert len(events(config, "postcommit_represented")) == 1
    assert orch.state.task_execution is None
    assert transcript_of(config).count('"task_pushed"') == 1
    wt_git = worktree_git_for(worktrees, task.id)
    dest_ref = f"refs/heads/{execution.task_branch}"
    assert wt_git.remote_ref_sha("origin", dest_ref) == execution.candidate_sha
    assert execution_store.load(task.id).published_sha == execution.candidate_sha


def test_a_stale_task_execution_mirror_never_re_presents_a_published_candidate(
    tmp_path,
):
    """FAIL-OPEN GUARD. `_unpublished_candidate` reads `state.task_execution`,
    which is display state — a half-written save or a hand-edited state file can
    leave it naming work that has already shipped. The execution RECORD is what
    decides, so a stale mirror produces the plain refusal and not a second
    presentation of a published commit."""
    orch, worktrees, execution_store, task, config, execution = (
        published_then_bare_push(tmp_path)
    )
    record = execution_store.load(task.id)
    assert record.published_sha, "precondition: the record knows it shipped"
    presentations = len(events(config, "postcommit_represented"))
    # The mirror the publish cleared, put back as a torn save would leave it.
    orch.state.task_execution = {
        "task_id": task.id,
        "task_branch": record.task_branch,
        "task_base_sha": record.task_base_sha,
        "candidate_sha": record.candidate_sha,
    }

    stale = send_packet(orch)
    deliver(orch, stale, push_naming(stale))
    orch._step_executing()

    assert len(events(config, "postcommit_represented")) == presentations
    assert transcript_of(config).count('"task_pushed"') == 1
    wt_git = worktree_git_for(worktrees, task.id)
    assert (
        wt_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}")
        == execution.candidate_sha
    )


# =============================================================================
# 3. WHAT IS NOT WIDENED
# =============================================================================


def test_the_denial_budget_still_binds_when_nothing_can_be_re_presented(tmp_path):
    """`max_policy_denials` is not raised, bypassed or reset by any of this.
    With a live candidate that CANNOT be rendered — here its worker repository
    is unnamed, so the presence probe cannot even be addressed — the loop falls
    back to the plain refusal, and four of those still end the run exactly as
    policy-01 made them.

    The fallback must also never PARK: `_rebuild_task_review_at_head`'s refusals
    park, which is why this path deliberately does not route through it."""
    (orch, _repo, _wt, execution_store, task, config, _packet, later) = (
        moved_past_the_packet(tmp_path)
    )
    execution = execution_store.load(task.id)
    execution.worktree_path = ""
    execution_store.save(execution)

    request = later
    for _ in range(DENIALS_TO_EXHAUST):
        deliver(orch, request, push_naming(request))
        orch._step_executing()
        if orch.state.phase != Phase.READY.value:
            # Named rather than inferred: a PARK here (which is what routing
            # this path through `_rebuild_task_review_at_head`'s refusals would
            # have produced) also leaves `ready`, and would otherwise surface as
            # a confusing count mismatch below rather than as itself.
            assert orch.state.phase == Phase.STOPPED.value
            break
        request = send_packet(orch)

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    assert [entry["code"] for entry in events(config, "stopped")] == [
        "policy_denial_budget_exhausted"
    ]
    assert orch.state.policy_denials == DENIALS_TO_EXHAUST
    assert denial_codes(config) == [LEGACY] * DENIALS_TO_EXHAUST
    assert events(config, "postcommit_represented") == []
    # Read from `stop_reason` (which quotes the last denial verbatim) and from
    # the transcript, NOT from `state.outbox`: `_step_ready` moves the outbox
    # onto the request it builds and leaves it `None`, so the last refusal the
    # reviewer actually saw is no longer sitting in that field by the time the
    # fourth denial ends the run.
    assert LEGACY_REASON in orch.state.stop_reason
    # The candidate is named and `revise` is offered — the 2026-08-20 remedy
    # survives for the state that still needs it.
    assert task.id in orch.state.stop_reason
    assert "reply `revise`" in orch.state.stop_reason
    assert task.id in events(config, "policy_denied")[0]["reason"]


def test_an_unrenderable_packet_falls_back_to_the_refusal(tmp_path, monkeypatch):
    """An over-cap patch is the reviewer's `split` question, not something to
    re-present, and a render failure is not evidence about the candidate. Either
    way the reviewer gets the refusal it got before — never a raise out of a
    dispatch, and never a park."""
    from autoloop import orchestrator as orchestrator_module

    (orch, _repo, _wt, _store, task, config, _packet, later) = moved_past_the_packet(
        tmp_path
    )

    def too_large(*args, **kwargs):
        raise DiffTooLargeError("range-diff is 900000 bytes, cap is 200000")

    monkeypatch.setattr(
        orchestrator_module, "build_review_packet_with_diff", too_large
    )

    deliver(orch, later, push_naming(later))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert denial_codes(config) == [LEGACY]
    assert events(config, "postcommit_represented") == []
    outbox = orch.state.outbox or ""
    assert "revise" in outbox and task.id in outbox


def test_commit_and_push_is_refused_with_the_shape_sentence_and_no_packet(tmp_path):
    """The retired-by-DECISION verbs are untouched. No packet could make one of
    them valid, so nothing is re-presented — and the sentence they draw is the
    original one, because for them a packet answering an existing candidate IS
    what a valid approval looks like."""
    (orch, _repo, _wt, execution_store, task, config, _packet, later) = (
        moved_past_the_packet(tmp_path)
    )
    execution = execution_store.load(task.id)

    stamp = {
        "request_id": later.request_id,
        "head_sha": later.head_sha,
        "report_sha256": later.report_sha256,
    }
    deliver(
        orch,
        later,
        block(
            {
                "version": 3,
                "decision": "commit_and_push",
                "reason": "approved",
                "commit": {"message": "do it", "paths": ["a.py"]},
                "reviewed": stamp,
            }
        ),
    )
    orch._step_executing()

    assert denial_codes(config) == [LEGACY]
    assert events(config, "postcommit_represented") == []
    outbox = orch.state.outbox or ""
    assert APPROVAL_SHAPE in outbox
    assert "policy_denied" in outbox
    assert execution_store.load(task.id).candidate_sha == execution.candidate_sha
