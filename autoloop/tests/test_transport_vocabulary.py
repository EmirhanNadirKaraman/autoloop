"""Where the shared transport vocabulary LIVES, and what deleting the browser
transport did not change.

`SubmitResult` and `SendOutcome` are the words every provider speaks: the
orchestrator branches on both on the codex path, which has no browser in it at
all. They were defined inside `autoloop/browser/` until brw-17 (2026-08-27) and
are defined in `autoloop/conversation.py` now, so retiring that transport took
nothing live with it.

`autoloop/browser/` is GONE as of brw-19a (2026-08-31): all seven modules
unlinked, and the two adapters that still reached `SubmitResult` through
`browser/chatgpt.py`'s re-export — `autoloop/codex/conversation.py` and
`autoloop/codex/app_server_conversation.py` — repointed at
`autoloop.conversation`, where the enum has actually lived since brw-17. This
file is the proof that it stays gone.

Five claims, and each is here because breaking it fails SILENTLY:

* **Identity, not equality.** Every caller compares with `is`
  (`orchestrator._submit_request`, `inbox.provider_asker`). Both enums subclass
  `str`, so a SECOND definition of `SubmitResult` would still satisfy
  `== "rejected"` — an equality-based test would stay green while every `is`
  check in the loop silently went False. The repointed imports must therefore
  yield the same object, not a copy.
* **The values are a STORAGE FORMAT.** `Orchestrator._submit_request` writes
  `SendOutcome.REJECTED.value` to `PendingRequest.last_send_outcome`, which
  lands in `state.json` and is read back on the next start. A renamed value
  misreads a resumed run whose state predates the rename, and nothing raises.
* **No PRODUCTION module imports the retired transport — none of them, and no
  exception.** Pinned by reading the source of every `.py` under `autoloop/`
  except `autoloop/tests/`, because an import that creeps back is invisible
  until the package is deleted underneath it. The scan read `autoloop/*.py`
  alone until brw-19a and deliberately skipped `autoloop/codex/`, whose two
  adapters were then outside every approved scope; that carve-out is what let
  the last two live imports sit green under a file whose whole job was to prove
  there were none. It is gone: the regex below matches `from ..browser.chatgpt
  import SubmitResult` exactly as it stood, so this scan would have gone red on
  both lines the day it was widened.
* **The package ships no module and none can be imported.** The scan above is a
  statement about IMPORTERS, and would read clean over a tree where every
  importer was repointed and the seven modules were left standing — which is
  most of this task's failure mode, not a hypothetical. Asserted separately, on
  the source files and on `import_module` both, because each alone fails open:
  a file left on disk that nothing imports yet is still a package waiting to be
  imported, and an import that fails could be failing for its own reasons.
* **Nor does the orchestrator ADVISE running one.** Added by brw-19c
  (2026-08-31): a module path handed to an operator in park text is not an
  import and never breaks a run, so every guard above passes it — it fails
  later, when someone follows it while their browser is already down. Scanned
  as source text, over `orchestrator.py` only; the two files that still carry
  the same path in a COMMENT are named under THE GAP below.

THE GAP THAT LEAVES, named here so it is visible rather than merely unscanned.
It is now prose only — no import and no module survives either half of it.

`autoloop/conversation.py:385` spells `python3 -m autoloop.browser.chrome_restart`
inside a COMMENT that QUOTES the park a `codex_cli` run wrongly wrote on
2026-08-22, as the reason `_TRANSPORT_REMEDIES` exists. `autoloop/config.py:364`
names the same module in a comment about what replaced the retired
`browser.restart_command` example. Nobody is being advised in either, and
nothing breaks — but they are the same literal string, both files are outside
brw-19a's approved paths, and that is why the advice scan below reads
`orchestrator.py` alone rather than the package: widened, it would go red on two
lines this round was not allowed to edit.

`autoloop/tests/` is deliberately not scanned for imports, here or before. A
test that imported the package would now fail at COLLECTION with
`ModuleNotFoundError` — loudly, in the run that introduced it — which is the
one failure mode a silent-drift scan exists to catch and this one cannot hide.
`test_conversation_retirement.py` additionally self-checks its own source,
because it held the last two live test imports.
"""

import importlib
import re
import sys
from pathlib import Path

import pytest

import autoloop
from autoloop.codex import app_server_conversation as codex_app_server_conversation
from autoloop.codex import conversation as codex_conversation
from autoloop.conversation import SendOutcome, SubmitResult

PACKAGE_DIR = Path(autoloop.__file__).resolve().parent

#: Matches an import statement, indented or not, that reaches into the browser
#: package — by relative path (`from .browser…` / `from ..browser…`, the forms
#: the live modules used) OR by absolute one (`from autoloop.browser…`,
#: `import autoloop.browser…`). Both, deliberately: brw-17's own DONE MEANS
#: greps for the relative form only, and a guard that watches one spelling is a
#: guard the other spelling walks past. Deliberately NOT matched: prose and
#: operator advice naming a module inside the package as a COMMAND LINE rather
#: than as a dependency. That is still the right exclusion — such a line is
#: wrong advice, not a broken import, and the two failures want different
#: guards — but the set it excuses is now empty in the modules scanned here, and
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

#: The LAST live import of the browser package inside `orchestrator.py`, which
#: brw-17 deliberately left standing and brw-19b removed together with the
#: recovery that called it. Spelled out rather than described so the inverted
#: test below names the exact line it refuses, and so a re-add is caught by text
#: as well as by attribute.
_REMOVED_BROWSER_IMPORT = "from .browser.playwright_session import attachable_page_targets"

#: The six submodules brw-19a unlinked, by dotted name. `autoloop.browser`
#: itself is checked separately: the seventh file is its `__init__.py`, and the
#: package name has one more way to survive than a module does (see
#: `test_the_retired_browser_package_ships_no_module_at_all`).
_RETIRED_MODULES = (
    "autoloop.browser.chatgpt",
    "autoloop.browser.chrome_restart",
    "autoloop.browser.observation",
    "autoloop.browser.playwright_session",
    "autoloop.browser.selectors",
    "autoloop.browser.session",
)


def _production_modules() -> list[Path]:
    """Every `.py` under `autoloop/` that is not a test and not bytecode.

    A LIST rather than a generator, and asserted about by its callers before it
    is read: a glob that matched nothing — a moved package, a renamed directory,
    a `PACKAGE_DIR` computed off the wrong file — reports "clean" about a tree
    it never opened, which is the fail-open shape this module exists to refuse
    in other people's code.
    """
    return sorted(
        path
        for path in PACKAGE_DIR.rglob("*.py")
        if "tests" not in path.relative_to(PACKAGE_DIR).parts
        and "__pycache__" not in path.parts
    )


# ---- the vocabulary is defined here, and the codex adapters see THIS object --


def test_the_vocabulary_is_defined_in_the_transport_neutral_module():
    assert SubmitResult.__module__ == "autoloop.conversation"
    assert SendOutcome.__module__ == "autoloop.conversation"


def test_every_import_path_yields_THE_SAME_enum_object():
    """`is` comparisons are what the orchestrator uses; a copy would pass `==`.

    Both codex adapters imported this enum from `browser/chatgpt.py`'s
    re-export until brw-19a and import it from `autoloop.conversation` now. The
    assertions are unchanged on purpose: the whole point of the re-export was
    that it preserved object identity, so a repointing that preserved it too
    changes nothing here — and one that did NOT would fail exactly here."""
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


# ---- the package itself is gone ----------------------------------------------


def test_the_retired_browser_package_ships_no_module_at_all():
    """brw-19a's own claim, and the one the import scan below cannot make.

    Two independent spellings, because each alone fails open:

    * **On disk.** A module left standing that nothing imports YET still passes
      every import scan in this file — it is a package waiting to be imported,
      not a package that is gone.
    * **Through the import system.** A file can be absent from
      `autoloop/browser/` and the name still resolve, from a stale artefact or
      from something else on `sys.path` shadowing it. `import_module` is the
      only check that answers the question a caller actually asks.

    `sys.modules` is checked BEFORE each import, so a module some earlier test
    in this process already imported is reported as the violation it is rather
    than quietly satisfying the raises-check from cache.

    The SOURCE FILES are asserted about, never the directory's existence. The
    executor that performs a deletion unlinks files and leaves the directory
    behind, and git does not track a directory, so `browser_dir.exists()` would
    grade a worker tree's leftovers instead of the committed tree — red on a
    correct change, which is worse than useless.
    """
    browser_dir = PACKAGE_DIR / "browser"
    survivors = sorted(p.name for p in browser_dir.glob("*.py")) if browser_dir.is_dir() else []
    assert survivors == [], (
        f"{browser_dir} still ships module source: {survivors} — the browser "
        "transport was retired in brw-16 and deleted in brw-19a"
    )

    for name in _RETIRED_MODULES:
        assert name not in sys.modules, f"{name} was imported by something in this session"
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)

    # The package NAME has one survival route a module does not: an emptied
    # directory left behind by an unlink resolves as a PEP 420 namespace
    # package, which imports fine and can hold no code. That is acceptable and
    # is exactly what a worker tree looks like between the deletion and the
    # commit; a REGULAR package — one with an `__init__.py`, and therefore with
    # code — is not.
    try:
        package = importlib.import_module("autoloop.browser")
    except ModuleNotFoundError:
        return
    assert getattr(package, "__file__", None) is None, (
        f"autoloop.browser imported as a real package from {package.__file__}"
    )


# ---- no production module imports the retired transport ----------------------


def test_no_production_module_imports_the_browser_package_AT_ALL():
    """brw-17's DONE MEANS with brw-19b's exception removed and brw-19a's
    carve-out closed, asserted on the source rather than on a symptom.

    Reads every `.py` under `autoloop/` except `autoloop/tests/` —
    `autoloop/codex/` and `autoloop/audit/` included, where this scan read
    `autoloop/*.py` alone until brw-19a.

    The population is asserted BEFORE the scan, in this same function, and per
    SUBTREE rather than only in total: a total that clears its floor while
    `codex/` contributes nothing is precisely the state the old carve-out
    described, and it must not be reachable by accident from a rename. With no
    allowed import left anywhere, an empty scan and a clean tree produce the
    identical empty offender list, so the floors are the only thing separating
    them.
    """
    scanned = _production_modules()
    top_level = [path for path in scanned if path.parent == PACKAGE_DIR]
    codex = [path for path in scanned if path.parent.name == "codex"]
    audit = [path for path in scanned if path.parent.name == "audit"]
    assert len(scanned) > 40, f"the scan read almost nothing — {PACKAGE_DIR} is wrong"
    assert len(top_level) > 20, "the package's own modules are missing from the scan"
    assert len(codex) >= 5, "autoloop/codex/ is missing from the scan — the brw-19a carve-out"
    assert len(audit) >= 5, "autoloop/audit/ is missing from the scan"
    for sentinel in (
        "orchestrator.py",
        "codex/conversation.py",
        "codex/app_server_conversation.py",
    ):
        assert (PACKAGE_DIR / sentinel) in scanned, f"{sentinel} is not being read"

    offenders = []
    for module in scanned:
        rel = module.relative_to(PACKAGE_DIR).as_posix()
        for lineno, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not _BROWSER_IMPORT.match(line):
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "production modules must not import the deleted browser transport; the "
        "shared vocabulary is in conversation.py and the package is gone: "
        f"{offenders}"
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
    docstring. That advice cost nothing at import time — it failed when someone
    FOLLOWED it, with `No module named`, at the one moment it was reached: the
    browser already down and the loop already stopped. Since brw-19a the module
    is not merely retired but deleted, so there is no longer any state of the
    world in which such a line could work.

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
        "operator-facing text must not name a module inside the deleted "
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
