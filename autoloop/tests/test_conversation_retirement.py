"""A send that never appears means the CONVERSATION is wedged, not the browser.

Observed 2026-08-17, 09:05–09:15, and it fooled every check that existed: the
composer was clickable, no throttle modal was up, Chrome was healthy with 12
CDP targets, and the account was demonstrably writing — an operator posted by
hand in a DIFFERENT conversation. The loop's own submission simply never
appeared (`submitted=True`, `send_attempted=True`, the chat pinned at 33
messages for ten minutes). The symptom surfaced as a locator timeout on the
message the loop believed it sent, which reads as a browser fault, so the
recovery chosen was RESTART CHROME — every 45 seconds, for ten minutes,
against a fault no restart could ever fix. Rotation fixed it in seconds, by
hand, because nothing classified the state.

WHAT IS LEFT OF THAT HERE, after brw-19c (2026-08-31): the ORCHESTRATOR half.

This file used to hold both halves. The client half ran the real
`BrowserChatGPT.await_response` over a scripted page and pinned how it
classified an absent submission — proven bounded absence versus an unmounted
tail, a dead browser, an unprovable absence, a throttle discovered mid-probe.
Every one of those tests imported `autoloop.browser.chatgpt` and
`autoloop.browser.selectors`, and that package is being retired; a test that
holds the last import of a module scheduled for deletion is not coverage, it
is a blocker wearing coverage's clothes. They are removed rather than
rewritten against a stand-in: a fake raising `ConversationUnusableError` by
fiat would prove only that the fake was written correctly, which is exactly
the echo those tests existed to avoid.

The RULE they fed is untouched, and it is what the tests below still pin at the
orchestrator, where it is enforced and where it is transport-independent:

* a `ConversationUnusableError` PARKS (`conversation_unusable`) WITHOUT
  restarting the browser and WITHOUT charging the browser failure budget
  (`consecutive_failures`) — the brw-03 rule: a budget that decides recovery is
  hopeless must not be spent on a fault no restart could fix;
* whatever it decides is durable before anything else runs;
* and the boundary case still routes the other way — a `SessionLostError` that
  ESCAPES a client keeps the restart-and-budget recovery, because a fresh chat
  cannot fix a browser nothing can attach to.

Until brw-15 (2026-08-25) the first bullet said "rotates": the fault opened a
replacement chat in the configured ChatGPT project and moved the request into
it. That machinery is gone, and the classification is what survives it — which
is the right way round, since the 2026-08-17 incident was a MISCLASSIFICATION
(ten minutes of 45-second Chrome restarts against a chat no restart could fix),
not a missing recovery. An operator now moves the loop by hand, which is what
the operator did on the day.
"""

from autoloop.errors import ConversationUnusableError, SessionLostError
from autoloop.state import LoopState, Phase, StateStore

from test_transport_recovery import (  # noqa: E402 - see conftest sys.path
    CONV_URL,
    PROJECT_URL,
    RotatingFakeClient,
    build,
    pending,
    transcript_entries,
)

# The orchestrators `build` returns select a browser-backed provider that only
# exists while this fixture has registered it (brw-16, 2026-08-25: no shipped
# provider is browser-backed). An autouse fixture applies to the module it is
# visible in, so importing the NAME is what carries it here — without it every
# test below would route through `_handle_transport_failure` and the
# `browser_error`/`browser_restarted` assertions would pass vacuously.
#
# It registers a provider name against a factory, and nothing in it reaches the
# browser package — which is why this import survived brw-19c while the client
# imports did not.
from test_transport_recovery import _browser_backed_provider  # noqa: E402,F401

# ---- at the orchestrator: park, no restart, no budget -----------------------
#
# The recovery this section asserted was ROTATION until brw-15 (2026-08-25)
# removed it. The rule it was written for is untouched and is what these tests
# still pin: a fault established THROUGH a working, un-throttled page must not
# spend the browser recovery — no Chrome restart, no `consecutive_failures`
# increment — because no restart could fix it. What changed is only where the
# fault goes afterwards: a `conversation_unusable` park naming the wedged chat,
# instead of a replacement chat opened automatically.


def _missing_submission_error():
    return ConversationUnusableError(
        "the submission this loop made (alr-test-0001) never appeared: the "
        "conversation was read to its end without finding it",
        code="submission_never_appeared",
    )


def _raise_missing_submission(client):
    raise _missing_submission_error()


def test_a_missing_submission_parks_without_restarting_or_charging_the_budget(tmp_path):
    """The whole point of the classification, and the half that outlives both
    the rotation and the transport that discovered it: the browser is never
    restarted (a restart command IS configured, so one would be visible if
    attempted), and the browser failure budget — the counter that decides
    recovery is hopeless — is not spent on a fault no restart could fix. The
    park names the chat and the error's own code, so the operator sees WHICH
    shape of unusable this was."""
    client = RotatingFakeClient(responses=[_raise_missing_submission])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)
    # A restart that DID happen would run this and log `browser_restarted`.
    # `("true",)` and not a module path: since brw-19c nothing in the package
    # ships a restart command, and this only has to be something a restart
    # would visibly reach for.
    object.__setattr__(config.browser, "restart_command", ("true",))

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.resume_phase == Phase.AWAITING.value
    # Nothing moved: the loop is still pinned to the conversation it condemned,
    # which is exactly what the park has to tell the operator.
    assert orch.state.rotations == 0
    assert orch.state.conversation_url == CONV_URL
    assert orch.state.conversation_epoch == 0
    assert orch.state.pending_request.conversation_url == CONV_URL
    # No restart, no browser-failure accounting.
    assert orch.state.consecutive_failures == 0
    assert orch.state.browser_restart_skips == 0
    assert transcript_entries(config, "browser_restarted") == []
    assert transcript_entries(config, "browser_error") == []
    assert transcript_entries(config, "conversation_rotated") == []
    unusable = transcript_entries(config, "conversation_unusable")
    assert unusable and unusable[0]["data"]["reason_code"] == "submission_never_appeared"
    question = orch.state.question or ""
    assert "submission_never_appeared" in question
    assert CONV_URL in question
    # And it never navigated anywhere to find a replacement.
    assert PROJECT_URL not in client.retargets
    assert client.submitted == []


def test_the_park_survives_a_restart_of_the_process(tmp_path):
    """Same durability rule the rotation had: whatever this fault decided is on
    disk before anything else runs, so the next process resumes on it rather
    than re-deciding from a state that never got written."""
    client = RotatingFakeClient(responses=[_raise_missing_submission])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    reloaded = StateStore(config.state_file).load()
    assert reloaded.phase == Phase.NEEDS_USER.value
    assert reloaded.rotations == 0
    assert reloaded.conversation_url == CONV_URL
    assert reloaded.pending_request.conversation_url == CONV_URL
    assert reloaded.last_rotation in (None, {})
    assert "submission_never_appeared" in (reloaded.question or "")


def test_a_dead_session_in_awaiting_still_takes_the_restart_path(tmp_path):
    """brw-11's state 3 boundary, from this phase: a `SessionLostError` that
    ESCAPES the client is one the client's own probe could not do better
    than — no attachable page, a sighted request, or unprovable absence. At the
    orchestrator that is a browser fault and keeps the restart-and-budget
    recovery: a fresh chat cannot fix a browser nothing can attach to.

    THE DISCRIMINATOR for the whole module lives in this test, which is why it
    outranks its own subject: it is the one case whose answer differs between a
    browser-backed transport and any other, so it is where the imported autouse
    fixture is proven to have arrived."""

    def _dead(client):
        raise SessionLostError("browser session lost (TimeoutError: ...)")

    client = RotatingFakeClient(responses=[_dead])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)
    # Since brw-16 (2026-08-25) the provider `build` names is browser-backed
    # only while `test_transport_recovery._browser_backed_provider` is
    # registered, and that autouse fixture reaches this file by IMPORT. Almost
    # every assertion in the tests above is a negative — no `browser_error`, no
    # `browser_restarted`, `browser_restart_skips == 0` — and every one of them
    # passes vacuously if the import stopped carrying the fixture and these runs
    # routed through `_handle_transport_failure` instead.
    assert orch._transport_is_browser_backed(), (
        "the imported autouse fixture is not registering the provider `build` names"
    )

    orch.run(max_steps=1)

    assert orch.state.rotations == 0
    assert orch.state.consecutive_failures == 1  # the ordinary budget, as before
    assert orch.state.phase == Phase.AWAITING.value  # retried with a fresh client
    assert transcript_entries(config, "conversation_rotated") == []


def test_this_module_no_longer_reaches_into_the_retired_browser_package():
    """The brw-19c claim about THIS file, asserted on its own source.

    Two spellings, because each alone fails open. The SOURCE check catches an
    import re-added under any alias — including one whose symbol this file
    never uses, which an attribute check cannot see. The MODULE check catches
    the reverse: a name that arrives through a helper import rather than a
    literal `from autoloop.browser...` line here.

    Written as a self-check rather than left to `test_transport_vocabulary.py`,
    whose scan covers `autoloop/*.py` and deliberately not `autoloop/tests/`:
    this file held the last two live test imports of that package, so nothing
    else was watching this particular door.
    """
    import sys
    from pathlib import Path

    source = Path(__file__).resolve().read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if line.lstrip().startswith(("from autoloop.browser", "import autoloop.browser"))
    ]
    assert offenders == [], offenders

    module = sys.modules[__name__]
    assert not hasattr(module, "BrowserChatGPT")
    assert not hasattr(module, "ChatGPTSelectors")
