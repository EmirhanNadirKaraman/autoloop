"""Where the shared transport vocabulary LIVES, and what the move did not change.

`SubmitResult` and `SendOutcome` are the words every provider speaks: the
orchestrator branches on both on the codex path, which has no browser in it at
all. They were defined inside `autoloop/browser/` until brw-17 (2026-08-27) and
are defined in `autoloop/conversation.py` now, so retiring the browser transport
takes nothing live with it.

Nothing in THIS file imports `autoloop.browser` any more either (brw-19b). It
used to, to prove the package still spoke the moved vocabulary — a claim about
a package that is being deleted, held in a file whose own job is to prove
nothing live depends on it.

Four claims, and each is here because breaking it fails SILENTLY:

* **Identity, not equality.** Every caller compares with `is`
  (`orchestrator._submit_request`, `inbox.provider_asker`). Both enums subclass
  `str`, so a SECOND definition of `SubmitResult` would still satisfy
  `== "rejected"` — an equality-based test would stay green while every `is`
  check in the loop silently went False. The re-export must therefore be the
  same object, not a copy.
* **The values are a STORAGE FORMAT.** `Orchestrator._submit_request` writes
  `SendOutcome.REJECTED.value` to `PendingRequest.last_send_outcome`, which
  lands in `state.json` and is read back on the next start. A renamed value
  misreads a resumed run whose state predates the rename, and nothing raises.
* **Live modules no longer import a retired transport — none of them, and no
  exception.** Pinned by reading the source of `autoloop/*.py`, because an
  import that creeps back is invisible until the package is deleted underneath
  it. There was ONE allowed import until brw-19b, the orchestrator's CDP
  page-target probe; it and the recovery that called it are gone, so the
  allowance is gone with them and the count this file permits is zero.
  `autoloop/codex/` is still NOT scanned: both adapters there reach
  `SubmitResult` through `browser/chatgpt.py`'s re-export, that package was
  outside brw-17's approved scope and outside brw-19b's, and repointing those
  two lines belongs to whoever owns them. Scanning it would fail on a state
  this file was told to leave standing.
* **Nor does the orchestrator ADVISE running one.** The fourth claim, added by
  brw-19c (2026-08-31): a module path handed to an operator in park text is not
  an import and never breaks a run, so every guard above passes it — it fails
  later, when someone follows it while their browser is already down. Scanned
  as source text, over `orchestrator.py` only; the file that still carries the
  same path in a comment is named under THE GAP below.

THE GAP THAT LEAVES, named here so it is visible rather than merely unscanned
(brw-19c, 2026-08-31). Those two lines are
`autoloop/codex/conversation.py:83` and
`autoloop/codex/app_server_conversation.py:62`, both
`from ..browser.chatgpt import SubmitResult`. They are LIVE imports on the
transports the loop actually runs, so deleting `autoloop/browser/` breaks both
adapters at import time — and this file stays green while it happens, because
the scan below deliberately does not read that directory. Repointing them at
`autoloop.conversation`, where the vocabulary has lived since brw-17, is a
one-line change each and belongs to whoever deletes the package. It is NOT
pinned by a test here on purpose: any test asserting those imports exist
inverts the moment they are fixed, which is the wrong way round for a thing
that ought to be fixed.

`autoloop/conversation.py:385` is the smaller half of the same gap, and a
milder one: it spells `python3 -m autoloop.browser.chrome_restart` inside a
COMMENT that QUOTES the park a `codex_cli` run wrongly wrote on 2026-08-22, as
the reason `_TRANSPORT_REMEDIES` exists. Nobody is being advised there and
nothing breaks — but it is the same literal string, it is outside brw-19c's
approved paths, and that is why the advice scan below reads `orchestrator.py`
alone rather than the package: widened, it would go red on a line this round
was not allowed to edit.
"""

import re
from pathlib import Path

import autoloop
from autoloop.codex import app_server_conversation as codex_app_server_conversation
from autoloop.codex import conversation as codex_conversation
from autoloop.conversation import SendOutcome, SubmitResult

PACKAGE_DIR = Path(autoloop.__file__).resolve().parent

#: Matches an import statement, indented or not, that reaches into the browser
#: package — by relative path (`from .browser…`, the form the live modules use)
#: OR by absolute one (`from autoloop.browser…`, `import autoloop.browser…`).
#: Both, deliberately: brw-17's own DONE MEANS greps for the relative form only,
#: and a guard that watches one spelling is a guard the other spelling walks
#: past. Deliberately NOT matched: prose and operator advice naming a module
#: inside the package as a COMMAND LINE rather than as a dependency. That is
#: still the right exclusion — such a line is wrong advice, not a broken
#: import, and the two failures want different guards — but the set it excuses
#: is now empty in the modules scanned here, and
#: `test_no_operator_advice_in_the_orchestrator_names_the_retired_package` is
#: the guard that keeps it empty.
#:
#: How it got there. `config.RESTART_COMMAND_REPLACEMENT` was the shipped
#: example for `browser.restart_command` and brw-19c (2026-08-31) deleted it,
#: because an example naming a module that is going away is advice that cannot
#: work. The same task then reworded the three places `orchestrator.py` spelled
#: `python3 -m autoloop.browser.chrome_restart` — two parks and the docstring
#: of `_browser_restart_outcome`, which stopped naming the module at all. The
#: two PARKS still name `chrome_restart`, as the obsolete thing it is, and send
#: the operator to their own `browser.restart_command` or to reopening the
#: profile's window instead. Naming it is not
#: decoration: `test_rounds_and_restart.py:1062`/`:1069` (the second over
#: `question[:160]`) and `test_transport_fault_recovery.py:908` all require the
#: word to survive in the park an operator reads, so a reword that simply
#: deleted it would turn three tests red and take away the one string an
#: operator who knows the old command would search for.
_BROWSER_IMPORT = re.compile(
    r"^\s*(?:from \.{1,2}|from autoloop\.|import autoloop\.)browser(?:[.\s]|$)"
)

#: The LAST live import of the browser package, which brw-17 deliberately left
#: standing and brw-19b removed together with the recovery that called it.
#: Spelled out rather than described so the inverted test below names the exact
#: line it refuses, and so a re-add is caught by text as well as by attribute.
_REMOVED_BROWSER_IMPORT = "from .browser.playwright_session import attachable_page_targets"


# ---- the vocabulary is defined here, and the codex adapters see THIS object --


def test_the_vocabulary_is_defined_in_the_transport_neutral_module():
    assert SubmitResult.__module__ == "autoloop.conversation"
    assert SendOutcome.__module__ == "autoloop.conversation"


def test_every_import_path_yields_THE_SAME_enum_object():
    """`is` comparisons are what the orchestrator uses; a copy would pass `==`.

    The two browser spellings were checked here until brw-19b and are not any
    more: this file no longer imports that package. What the codex adapters
    reach is unchanged, and that is the half the loop actually runs."""
    assert codex_conversation.SubmitResult is SubmitResult
    assert codex_app_server_conversation.SubmitResult is SubmitResult
    assert codex_conversation.SubmitResult.REJECTED is SubmitResult.REJECTED


def test_submit_result_members_and_values_are_unchanged():
    assert [member.name for member in SubmitResult] == [
        "ALREADY_PERSISTED",
        "CONFIRMED",
        "UNCONFIRMED",
        "REJECTED",
    ]
    assert SubmitResult.ALREADY_PERSISTED.value == "already_persisted"
    assert SubmitResult.CONFIRMED.value == "confirmed"
    assert SubmitResult.UNCONFIRMED.value == "unconfirmed"
    assert SubmitResult.REJECTED.value == "rejected"


def test_send_outcome_members_and_values_are_unchanged():
    """These three strings are what `state.json` holds — see the module
    docstring. A rename here breaks a resumed run, not this test's own file."""
    assert [member.name for member in SendOutcome] == [
        "ACCEPTED",
        "REJECTED",
        "UNKNOWN",
    ]
    assert SendOutcome.ACCEPTED.value == "accepted"
    assert SendOutcome.REJECTED.value == "rejected"
    assert SendOutcome.UNKNOWN.value == "unknown"


def test_both_are_still_str_enums_so_a_persisted_value_compares_equal():
    """The `str` mixin is why `req.last_send_outcome == SendOutcome.REJECTED.value`
    reads a plain string out of state.json and still matches."""
    assert issubclass(SubmitResult, str)
    assert issubclass(SendOutcome, str)
    assert SendOutcome("rejected") is SendOutcome.REJECTED
    assert SubmitResult("unconfirmed") is SubmitResult.UNCONFIRMED


# ---- no live module imports the retired transport ----------------------------


def test_no_top_level_module_imports_the_browser_package_AT_ALL():
    """brw-17's DONE MEANS with brw-19b's exception removed, asserted on the
    source rather than on a symptom.

    Reads every `autoloop/*.py` — the package's own live modules, not
    `autoloop/browser/` (which may import itself), not `autoloop/codex/` (see
    the module docstring) and not `autoloop/tests/`.

    The population is asserted BEFORE the scan, in this same function: a glob
    that matched nothing (a moved package, a renamed directory) would report
    "clean" about a tree it never read, which is the fail-open shape this test
    exists to catch in other people's code. It is load-bearing here in a way it
    was not before: with no allowed import left, an empty scan and a clean tree
    produce the identical empty offender list."""
    scanned = sorted(PACKAGE_DIR.glob("*.py"))
    assert len(scanned) > 20, f"the scan read almost nothing — {PACKAGE_DIR} is wrong"
    assert (PACKAGE_DIR / "orchestrator.py") in scanned, "the module that held the last one"
    offenders = []
    for module in scanned:
        for lineno, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not _BROWSER_IMPORT.match(line):
                continue
            offenders.append(f"{module.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "live modules must not import the retired browser transport; move the "
        f"shared name into conversation.py instead: {offenders}"
    )


def test_the_last_browser_import_and_everything_it_fed_are_gone():
    """The inversion of what this file pinned until brw-19b, and the reason the
    scan above cannot go green by looking at the wrong tree.

    Three spellings of one claim, because each alone fails open: the exact
    import LINE is absent (a re-add under a different alias still matches the
    regex above, but this catches the literal restoration); the module has no
    such NAME bound (which catches an alias the line-text check would miss);
    and the orchestrator has no probe METHOD (which catches a hand-rolled
    reimplementation that imports nothing at all)."""
    from autoloop import orchestrator as orchestrator_module
    from autoloop.orchestrator import Orchestrator

    orchestrator_src = (PACKAGE_DIR / "orchestrator.py").read_text(encoding="utf-8")
    assert _REMOVED_BROWSER_IMPORT not in orchestrator_src
    assert not hasattr(orchestrator_module, "attachable_page_targets")
    assert not hasattr(Orchestrator, "_attachable_page_targets")


# ---- nor does it tell an operator to RUN one ---------------------------------


def test_no_operator_advice_in_the_orchestrator_names_the_retired_package():
    """The other way a deleted package outlives itself: as a COMMAND LINE in
    text handed to an operator, which no import guard can see.

    Until brw-19c (2026-08-31) `orchestrator.py` told operators to run
    `python3 -m autoloop.browser.chrome_restart` in two parks and one
    docstring. That advice costs nothing at import time — it fails when someone
    FOLLOWS it, with `No module named`, at the one moment it is reached: the
    browser is already down and the loop has already stopped.

    Source text rather than a built park, deliberately. Both parks need a
    browser-backed transport and a fault injected at the right step;
    `test_rounds_and_restart.py` and `test_transport_fault_recovery.py` pay
    that cost and pin what the parks DO say. What is cheap here — and what
    those tests cannot see, because they only read the two messages they
    already know about — is a FOURTH mention appearing in some message nothing
    happens to assert on."""
    source = (PACKAGE_DIR / "orchestrator.py").read_text(encoding="utf-8")
    # The scan is worth exactly as much as the file behind it: a moved or
    # emptied orchestrator.py would satisfy every assertion below by reading
    # nothing, which is the shape this file exists to refuse elsewhere.
    assert "_browser_restart_outcome" in source, "wrong file, or an empty one"

    offenders = [
        f"orchestrator.py:{lineno}: {line.strip()}"
        for lineno, line in enumerate(source.splitlines(), start=1)
        if "autoloop.browser" in line
    ]
    assert offenders == [], (
        "operator-facing text must not name a module inside the retired "
        f"package; say what to configure or do instead: {offenders}"
    )

    # The name SURVIVES, as the obsolete thing it is. Dropping it would read as
    # the helper never existing, to the operator most likely to type it — and
    # `test_rounds_and_restart.py:1062`/`:1069` and
    # `test_transport_fault_recovery.py:908` require the word in the park text.
    # The window is over raw source, so it spans the `"` `\n` `"` seams of the
    # concatenated literals each message is built from.
    windows = [
        source[match.start() : match.start() + 240]
        for match in re.finditer("chrome_restart", source)
    ]
    assert len(windows) >= 2, "both parks must still name what went obsolete"
    for window in windows:
        assert "obsolete" in window, (
            "every surviving mention must say the helper is obsolete, not "
            f"read as an instruction: {window[:120]!r}"
        )
