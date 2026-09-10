"""A decomposition must not order a part before the work that makes it possible.

split-06. THE ONE CLAIM these tests grade: when a part of a split plan holds a
Python module whose importer belongs to a part that does not run first, the loop
SAYS SO — naming the specific edge — before the plan is accepted; and when it
cannot say, it accepts the plan unchanged and records that it did not look.

The measurement behind it (2026-08-27). `brw-19` was decomposed into
`brw-19a..e` with the order inverted:

    brw-19a  delete autoloop/browser/     deps: []          <- ran FIRST
    brw-19b  remove recovery machinery    deps: [brw-19a]   <- holds orchestrator.py
    brw-19c  remove browser config        deps: [brw-19a]   <- holds test_conversation_retirement.py

The only three files importing the package belonged to brw-19b and brw-19c, both
gated BEHIND brw-19a, so deleting the package made `import autoloop.orchestrator`
an ImportError and every path that could repair it was outside brw-19a's scope.
Four attempts, four review rounds, the attempt ceiling, a `task_fatal` park and
an operator inverting the chain by hand — spent discovering something a static
read of the import graph could have said before the plan was accepted. §1
replays that decomposition as a fixture.

What breaks the claim if left untested, one section each:

  * the FLAG itself, and the named edge — a warning nobody can act on is the
    panel this repository refuses to build elsewhere (§1);
  * the NEGATIVES, which is where an over-eager check turns into noise: correct
    orderings, a part holding both ends, transitive dependencies (§2);
  * FAIL OPEN — an unbuildable graph, a TRUNCATED one (the branch that would
    otherwise silently pass), unreadable parts. Every one of these must accept
    the plan AND record that nothing was checked; a test asserting only "no
    warnings" passes on a check that is silently broken (§3);
  * the analysis must stay a pure function of the tree, and must reuse the
    graph the repository already builds rather than a second one (§4);
  * the warning must actually REACH the split record, the reviewer and the
    parts' briefs — and so must the DID-NOT-RUN notice, which names no edge and
    would otherwise be the one outcome that reached a brief as silence — while
    split-01's atomic acceptance stays untouched by any of it (§5).

§1-§4 build a plain directory of `.py` files: `build_import_graph` reads a tree,
not a repository, so a git repo there would be six subprocesses proving nothing
about the claim. §5 uses the real orchestrator wiring from `test_task_split.py`,
because what it claims is about what ends up in the registry, on disk and in the
transcript.
"""

from __future__ import annotations

import json

import pytest

from test_task_split import (
    ask_at_ceiling,
    block,
    build,
    dispatch,
    first_round,
    ready_task,
    records,
)

from autoloop import validation
from autoloop.state import Phase
from autoloop.validation import (
    SPLIT_ORDER_MAX_EDGES,
    SplitOrderReport,
    split_order_warnings,
)


# ---------------------------------------------------------------------------
# fixtures: a tree, and parts over it
# ---------------------------------------------------------------------------


class Part:
    """The shape `split_order_warnings` reads — `contract.TaskSpec` and
    `tasks.Task` both carry exactly these three attributes, and the analysis
    reads nothing else off a part."""

    def __init__(self, part_id, depends_on=(), approved_paths=()):
        self.id = part_id
        self.depends_on = tuple(depends_on)
        self.approved_paths = tuple(approved_paths)


def write_tree(root, files):
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


#: The brw-19 checkout, reduced to the files the failure was actually about:
#: the package, the top-level import in `orchestrator.py` that made deleting it
#: an ImportError, and the two tests that imported it directly.
BRW_19_TREE = {
    "autoloop/__init__.py": "",
    "autoloop/browser/__init__.py": "",
    "autoloop/browser/playwright_session.py": "def attachable_page_targets():\n    return ()\n",
    "autoloop/orchestrator.py": (
        "from .browser.playwright_session import attachable_page_targets\n"
        "\n"
        "USED = attachable_page_targets\n"
    ),
    "autoloop/config.py": "BROWSER = {}\n",
    "autoloop/tests/test_conversation_retirement.py": (
        "from autoloop.browser.playwright_session import attachable_page_targets\n"
        "\n"
        "def test_it():\n    assert attachable_page_targets() == ()\n"
    ),
    "autoloop/tests/test_transport_recovery.py": (
        "from autoloop.browser import playwright_session\n"
        "\n"
        "def test_it():\n    assert playwright_session\n"
    ),
}

#: brw-19's actual ordering: the deletion first, its importers behind it.
BRW_19_INVERTED = (
    Part("brw-19a", approved_paths=("autoloop/browser/",)),
    Part(
        "brw-19b",
        depends_on=("brw-19a",),
        approved_paths=("autoloop/orchestrator.py", "autoloop/tests/test_transport_recovery.py"),
    ),
    Part(
        "brw-19c",
        depends_on=("brw-19a",),
        approved_paths=(
            "autoloop/config.py",
            "autoloop/tests/test_conversation_retirement.py",
        ),
    ),
)

#: The chain an operator had to invert by hand: the importers first, the
#: deletion behind them.
BRW_19_CORRECTED = (
    Part("brw-19a", depends_on=("brw-19b", "brw-19c"), approved_paths=("autoloop/browser/",)),
    Part(
        "brw-19b",
        approved_paths=("autoloop/orchestrator.py", "autoloop/tests/test_transport_recovery.py"),
    ),
    Part(
        "brw-19c",
        approved_paths=(
            "autoloop/config.py",
            "autoloop/tests/test_conversation_retirement.py",
        ),
    ),
)


@pytest.fixture
def brw19(tmp_path):
    return write_tree(tmp_path / "checkout", BRW_19_TREE)


# ---------------------------------------------------------------------------
# §1  the brw-19 decomposition, replayed
# ---------------------------------------------------------------------------


def test_the_brw_19_ordering_is_flagged(brw19):
    """THE claim, in the shape that cost a full task."""
    report = split_order_warnings(brw19, BRW_19_INVERTED)

    assert report.ran is True
    assert report.not_run_reason == ""
    assert report.flagged is True


def test_the_flag_names_the_orchestrator_edge_specifically(brw19):
    """A warning nobody can act on is the panel this repository already refuses
    to build. The edge that mattered — the TOP-LEVEL import in
    `orchestrator.py`, which is what turned the whole suite red — must be named
    with its module, its importer, the part that holds each, and the direction
    of the dependency that inverts them."""
    report = split_order_warnings(brw19, BRW_19_INVERTED)
    named = [edge.describe() for edge in report.edges]

    match = [
        line
        for line in named
        if "autoloop/orchestrator.py" in line
        and "autoloop/browser/playwright_session.py" in line
    ]
    assert match, named
    line = match[0]
    assert line.startswith("brw-19a holds autoloop/browser/playwright_session.py;")
    assert "autoloop/orchestrator.py imports it" in line
    assert "is in brw-19b" in line
    assert "which depends on brw-19a" in line


def test_both_gated_test_files_are_named_too(brw19):
    """All three importers of the retired package, not just the loudest one."""
    named = " ".join(
        edge.describe() for edge in split_order_warnings(brw19, BRW_19_INVERTED).edges
    )

    assert "autoloop/tests/test_transport_recovery.py" in named
    assert "autoloop/tests/test_conversation_retirement.py" in named


def test_the_rendered_advisory_says_what_to_do_about_it(brw19):
    """`describe()` is the ONE renderer the transcript, the reviewer's report
    and the parts' briefs all read, so what it says is what every reader gets."""
    text = split_order_warnings(brw19, BRW_19_INVERTED).describe()

    assert text.startswith("SPLIT-ORDER WARNING")
    assert "Re-order the parts" in text
    assert "advisory" in text
    assert "brw-19a holds autoloop/browser/playwright_session.py" in text


def test_two_parts_unordered_against_each_other_are_flagged_as_such(tmp_path):
    """The dependency need not be INVERTED to be wrong — two parts with no
    ordering between them are equally unsafe, and read differently to whoever
    has to fix it."""
    root = write_tree(tmp_path / "checkout", BRW_19_TREE)
    parts = (
        Part("p-mod", approved_paths=("autoloop/browser/",)),
        Part("p-imp", approved_paths=("autoloop/orchestrator.py",)),
    )

    report = split_order_warnings(root, parts)

    assert report.flagged is True
    assert all(edge.inverted is False for edge in report.edges)
    assert "is not ordered after p-mod" in report.describe()


# ---------------------------------------------------------------------------
# §2  what must NOT be flagged
# ---------------------------------------------------------------------------


def test_the_corrected_ordering_is_not_flagged(brw19):
    """The same plan with the dependency the other way round — the chain the
    operator produced by hand. If this flagged, the check would be telling the
    reviewer to undo the fix."""
    report = split_order_warnings(brw19, BRW_19_CORRECTED)

    assert report.ran is True
    assert report.flagged is False
    assert report.edges == ()
    assert report.describe() == ""


def test_a_part_holding_the_module_and_its_importers_is_not_flagged(brw19):
    """One part owning both ends can do to the importer whatever it does to the
    module. There is no ordering question to answer."""
    parts = (
        Part(
            "p-all",
            approved_paths=(
                "autoloop/browser/",
                "autoloop/orchestrator.py",
                "autoloop/tests/",
            ),
        ),
        Part("p-other", depends_on=("p-all",), approved_paths=("autoloop/config.py",)),
    )

    report = split_order_warnings(brw19, parts)

    assert report.flagged is False


def test_a_dependency_reached_through_another_part_is_not_flagged(brw19):
    """TRANSITIVE, not direct. A -> B -> C with C holding the importer is a
    correctly ordered plan: C runs first. A direct-only reading would report it
    as a fault, which is a false alarm on a plan that is right."""
    parts = (
        Part("p-a", depends_on=("p-b",), approved_paths=("autoloop/browser/",)),
        Part("p-b", depends_on=("p-c",), approved_paths=("autoloop/config.py",)),
        Part("p-c", approved_paths=("autoloop/orchestrator.py", "autoloop/tests/")),
    )

    report = split_order_warnings(brw19, parts)

    assert report.flagged is False


def test_an_importer_outside_the_whole_plan_is_counted_and_not_flagged(brw19):
    """The ordinary shape of an EDIT: a part holds a module and the files that
    import it are not in the plan at all. No re-ordering of this plan could
    answer that, and flagging it would fire on nearly every split — the panel
    nobody can act on. It is COUNTED, so the report does not pretend it saw
    nothing."""
    parts = (
        Part("p-mod", approved_paths=("autoloop/browser/",)),
        Part("p-doc", depends_on=("p-mod",), approved_paths=("autoloop/config.py",)),
    )

    report = split_order_warnings(brw19, parts)

    assert report.flagged is False
    assert report.edges == ()
    assert report.unowned_importers > 0


def test_a_plan_that_holds_no_python_module_is_not_flagged(brw19):
    """Nothing to analyse is not the same as nothing found, but it must not be a
    warning either."""
    parts = (
        Part("p-one", approved_paths=("docs/A.md",)),
        Part("p-two", depends_on=("p-one",), approved_paths=("docs/B.md",)),
    )

    report = split_order_warnings(brw19, parts)

    assert report.ran is True
    assert report.flagged is False


def test_a_part_with_no_approved_paths_owns_nothing(brw19):
    """An unscoped part is undispatchable (`_dispatch_task_postcommit` refuses
    it), and it certainly holds no module. Reading an empty scope as "owns
    everything" would flag every plan that contained one."""
    parts = (
        Part("p-empty"),
        Part("p-mod", depends_on=("p-empty",), approved_paths=("autoloop/browser/",)),
    )

    report = split_order_warnings(brw19, parts)

    assert report.ran is True
    assert report.flagged is False


def test_an_exact_path_does_not_own_its_siblings(brw19):
    """The ownership rule is `tasks.unauthorized_paths`, reached through
    `paths_within_scope` — the SAME matcher the scope gate enforces. An exact
    entry authorizes that file and nothing else, so a part naming
    `autoloop/browser/__init__.py` does not hold `playwright_session.py` and
    cannot be reported as ordering it."""
    parts = (
        Part("p-init", approved_paths=("autoloop/browser/__init__.py",)),
        Part("p-imp", depends_on=("p-init",), approved_paths=("autoloop/orchestrator.py",)),
    )

    report = split_order_warnings(brw19, parts)

    named = " ".join(edge.describe() for edge in report.edges)
    assert "autoloop/browser/__init__.py" in named
    assert "playwright_session.py" not in named


# ---------------------------------------------------------------------------
# §3  fail open — deliberately, and never silently
#
# The opposite of the usual rule in `validation.py`, because this is advisory
# analysis of a PLAN rather than a gate on correctness. Each case asserts BOTH
# halves: the plan is accepted unchanged AND the report says the check did not
# run. A case asserting only "not flagged" would pass on a check that had
# quietly switched itself off, which is the exact failure being guarded.
# ---------------------------------------------------------------------------


def test_an_unbuildable_graph_accepts_the_plan_and_says_it_did_not_run(brw19, monkeypatch):
    def boom(root):
        raise OSError("the checkout went away")

    monkeypatch.setattr(validation, "build_import_graph", boom)

    report = split_order_warnings(brw19, BRW_19_INVERTED)

    assert report.ran is False
    assert report.flagged is False
    assert "import graph could not be built" in report.not_run_reason
    assert "the checkout went away" in report.not_run_reason
    assert report.describe().startswith("SPLIT-ORDER CHECK DID NOT RUN")
    assert "accepted unchanged" in report.describe()


def test_a_truncated_graph_is_a_did_not_run_and_not_a_clean_bill(brw19, monkeypatch):
    """THE fail-open trap. A walk that hits `_GRAPH_MAX_FILES` returns
    `importers={}`, so every lookup answers "nothing imports this" and the
    whole plan reads clean — a check that silently passes exactly where the
    checkout is largest. This is the SAME tree §1 flags, so a missing guard
    here shows up as `ran is True` with no edges rather than as an error."""
    monkeypatch.setattr(validation, "_GRAPH_MAX_FILES", 1)

    report = split_order_warnings(brw19, BRW_19_INVERTED)

    assert report.ran is False
    assert report.flagged is False
    assert "above the import walk's cap" in report.not_run_reason
    # And with the cap back where it belongs, the same tree and the same plan
    # are flagged — so the assertion above is about the cap and not about the
    # fixture being empty.
    monkeypatch.undo()
    assert split_order_warnings(brw19, BRW_19_INVERTED).flagged is True


def test_a_part_whose_scope_cannot_be_read_accepts_the_plan(brw19):
    class Unreadable:
        depends_on = ()
        approved_paths = ()

        @property
        def id(self):
            raise RuntimeError("this row is not a part")

    report = split_order_warnings(brw19, (Part("p-ok"), Unreadable()))

    assert report.ran is False
    assert report.flagged is False
    assert "RuntimeError" in report.not_run_reason
    assert "this row is not a part" in report.not_run_reason


def test_a_part_whose_paths_are_not_a_list_accepts_the_plan(brw19):
    """`approved_paths` is validated on the way into the registry, but this runs
    BEFORE `add_many` — so a malformed spec reaches it, and must not take the
    acceptance down with it."""
    report = split_order_warnings(brw19, (Part("p-a", approved_paths=()), Part("p-b")))
    assert report.ran is True

    bad = Part("p-b")
    bad.approved_paths = 7
    report = split_order_warnings(brw19, (Part("p-a"), bad))

    assert report.ran is False
    assert report.flagged is False
    assert "TypeError" in report.not_run_reason


@pytest.mark.parametrize("field", ["approved_paths", "depends_on"])
def test_a_bare_string_where_a_list_belongs_is_refused_rather_than_iterated(brw19, field):
    """THE silent shape, and the only one in this function that fails quietly
    rather than loudly: a string iterates into single characters, raises
    nothing, and leaves the part owning no module and depending on nothing —
    `ran=True`, no edge, and the alarm never fires. Asserted on `ran`, because
    `flagged is False` holds on both sides of that bug."""
    bad = Part("p-mod", approved_paths=("autoloop/browser/",))
    setattr(bad, field, "autoloop/browser/")

    report = split_order_warnings(
        brw19, (bad, Part("p-imp", approved_paths=("autoloop/orchestrator.py",)))
    )

    assert report.ran is False
    assert report.flagged is False
    assert field in report.not_run_reason
    assert "bare str" in report.not_run_reason


def test_a_missing_checkout_is_a_did_not_run_and_not_a_clean_bill(tmp_path):
    """`_python_files` walks with `os.walk`, which yields NOTHING for a path
    that does not exist and raises nothing — so an unreachable checkout would
    otherwise produce an empty graph, no edges, and a report indistinguishable
    from a clean plan. Asserted on `ran`, because `flagged is False` holds on
    both sides of that bug."""
    report = split_order_warnings(tmp_path / "nowhere", BRW_19_INVERTED)

    assert report.ran is False
    assert report.flagged is False
    assert "not a readable directory" in report.not_run_reason
    assert report.describe().startswith("SPLIT-ORDER CHECK DID NOT RUN")


def test_a_checkout_that_is_a_file_is_a_did_not_run(tmp_path):
    not_a_tree = tmp_path / "checkout.txt"
    not_a_tree.write_text("not a checkout\n", encoding="utf-8")

    report = split_order_warnings(not_a_tree, BRW_19_INVERTED)

    assert report.ran is False
    assert "not a readable directory" in report.not_run_reason


def test_a_plan_with_no_parts_accepts_and_says_so(brw19):
    report = split_order_warnings(brw19, ())

    assert report.ran is False
    assert "no parts" in report.not_run_reason


def test_a_did_not_run_report_never_renders_as_silence():
    """The whole of "say when it did not run": every `ran=False` report has
    words for a human, whatever the reason was."""
    assert SplitOrderReport(ran=False, not_run_reason="anything").describe() != ""
    assert SplitOrderReport(ran=False).describe().startswith("SPLIT-ORDER CHECK DID NOT RUN")


# ---------------------------------------------------------------------------
# §4  bounds, determinism, and reusing the graph this repository already builds
# ---------------------------------------------------------------------------


def test_the_analysis_builds_the_import_graph_exactly_once(brw19, monkeypatch):
    """`validation.build_import_graph` already computes `importers[path]` in the
    direction risk travels. A second graph would be a second answer to one
    question, and this parses the whole checkout."""
    calls = []
    real = validation.build_import_graph

    def counted(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(validation, "build_import_graph", counted)
    split_order_warnings(brw19, BRW_19_INVERTED)

    assert len(calls) == 1


def test_the_named_edges_are_sorted_and_do_not_move_between_runs(brw19):
    """`graph.importers` holds frozensets and string hashing is randomised per
    process, so an unsorted list would put a different order into the
    transcript, the reviewer's report and the briefs on every run of the same
    plan."""
    first = [e.describe() for e in split_order_warnings(brw19, BRW_19_INVERTED).edges]
    second = [e.describe() for e in split_order_warnings(brw19, BRW_19_INVERTED).edges]

    assert first == second
    assert first == sorted(first)


def test_the_edge_list_is_capped_and_says_how_many_it_left_out(brw19):
    report = split_order_warnings(brw19, BRW_19_INVERTED, max_edges=2)

    assert len(report.edges) == 2
    assert report.omitted > 0
    assert f"{report.omitted} more edge(s)" in report.describe()


def test_a_cap_of_zero_still_reports_the_plan_as_flagged(brw19):
    """The cap is on what is RENDERED. A capped report that read as a clean one
    would be the silent pass wearing a different hat."""
    report = split_order_warnings(brw19, BRW_19_INVERTED, max_edges=0)

    assert report.edges == ()
    assert report.omitted > 0
    assert report.flagged is True
    assert report.describe().startswith("SPLIT-ORDER WARNING")


def test_a_cyclic_plan_terminates_rather_than_recursing(brw19):
    """The plan is UNVALIDATED here — `add_many`/`_check_acyclic` have not seen
    it — so a `depends_on` cycle is reachable input."""
    parts = (
        Part("p-a", depends_on=("p-b",), approved_paths=("autoloop/browser/",)),
        Part("p-b", depends_on=("p-a",), approved_paths=("autoloop/orchestrator.py",)),
    )

    report = split_order_warnings(brw19, parts)

    assert report.ran is True
    assert report.flagged is False


def test_the_default_cap_is_the_published_constant(brw19):
    assert SPLIT_ORDER_MAX_EDGES >= 1
    capped = split_order_warnings(brw19, BRW_19_INVERTED)
    explicit = split_order_warnings(brw19, BRW_19_INVERTED, max_edges=SPLIT_ORDER_MAX_EDGES)
    assert [e.describe() for e in capped.edges] == [e.describe() for e in explicit.edges]


# ---------------------------------------------------------------------------
# §5  the loop: where the warning goes, and what it must not disturb
# ---------------------------------------------------------------------------


def spec(part_id, deps=(), paths=("docs/A.md",)):
    return {
        "id": part_id,
        "title": f"Subtask {part_id}",
        "description": "one independently reviewable piece",
        "depends_on": list(deps),
        "approved_paths": list(paths),
    }


def plan(specs):
    return block(
        {
            "version": 3,
            "decision": "plan",
            "reason": "the objections keep relocating",
            "tasks": list(specs),
        }
    )


#: The inverted plan, expressed as the wire specs a reviewer would send.
INVERTED_SPECS = [
    spec("t1-a", paths=("autoloop/browser/",)),
    spec("t1-b", deps=("t1-a",), paths=("autoloop/orchestrator.py",)),
]

#: The same two parts, ordered so the importer's part runs first.
CORRECTED_SPECS = [
    spec("t1-a", deps=("t1-b",), paths=("autoloop/browser/",)),
    spec("t1-b", paths=("autoloop/orchestrator.py",)),
]


def at_the_ceiling_with_a_python_tree(tmp_path):
    """A wiring whose task `t1` is standing at its attempt ceiling, over a
    checkout that actually contains the brw-19 import edge.

    The tree is written AFTER the round that produced the candidate: the worker
    repositories are clones taken before this point, and the split acceptance
    reads the main checkout rather than committing to it.
    """
    wiring = first_round(tmp_path, tasks=[ready_task("t1")])
    ask_at_ceiling(wiring)
    write_tree(wiring.git.repo_root, BRW_19_TREE)
    return wiring


def split_records(wiring):
    return records(wiring, "task_ceiling_split")


def test_the_warning_reaches_the_record_the_reviewer_and_every_brief(tmp_path):
    """THE three destinations, graded off ONE acceptance. They are asserted
    together rather than in three tests because each round of this harness is a
    real git repository and a real orchestrator step, and CLAUDE.md's cost rule
    is explicit that a test which could have shared a fixture is a cost every
    round pays forever. What each destination is FOR:

      * the transcript, the durable half — the `SplitIntent` marker is cleared
        the moment the three stores agree, so it is not where a record lives;
      * the reviewer, the only actor who can RE-ORDER the plan;
      * every successor's `description`, which `implement_executor.
        _agent_prompt` puts straight into the prompt — and every one of them,
        not only the parts an edge names, because which child is dispatched
        first is the scheduler's decision and the point is that the FIRST agent
        to run reads the inverted edge instead of rediscovering it.

    The persisted registry is read back too: the child is dispatched by a later
    loop, out of `tasks.json`, so a brief that lived only in memory is no brief
    at all. `json.dumps` re-encodes the event because the transcript is JSONL —
    a value that cannot be encoded loses the whole record.
    """
    wiring = at_the_ceiling_with_a_python_tree(tmp_path)

    dispatch(wiring, plan(INVERTED_SPECS))

    entries = split_records(wiring)
    assert len(entries) == 1
    data = entries[0]
    assert data["split_order_ran"] is True
    assert data["split_order_not_run_reason"] == ""
    named = " ".join(data["split_order_edges"])
    assert "t1-a holds autoloop/browser/playwright_session.py" in named
    assert "autoloop/orchestrator.py imports it" in named
    assert "is in t1-b, which depends on t1-a" in named
    assert "split_order_edges" in json.dumps(data)

    outbox = wiring.orch.state.outbox or ""
    assert "DECOMPOSITION APPLIED" in outbox
    assert "SPLIT-ORDER WARNING" in outbox
    assert "which depends on t1-a" in outbox

    reloaded = wiring.task_store.load()
    for child_id in ("t1-a", "t1-b"):
        brief = wiring.registry.get(child_id).description
        assert brief.startswith("one independently reviewable piece")
        assert "SPLIT-ORDER WARNING" in brief
        assert "t1-a holds autoloop/browser/playwright_session.py" in brief
        assert "autoloop/orchestrator.py imports it" in brief
        assert reloaded.get(child_id).description == brief


def test_a_flagged_plan_is_applied_in_full_rather_than_refused(tmp_path):
    """WARN, NOT REFUSE — and the split-01 guarantee is that acceptance is
    atomic across the registry, the execution record and the worker repository.
    Both are asserted here at once, because the way this change could break
    either is the same: doing something between the last refusal and the
    durable marker."""
    wiring = at_the_ceiling_with_a_python_tree(tmp_path)

    dispatch(wiring, plan(INVERTED_SPECS))

    parent = wiring.registry.get("t1")
    assert parent.status == "retired"
    assert parent.superseded_by == ("t1-a", "t1-b")
    assert {"t1-a", "t1-b"} <= {t.id for t in wiring.registry.all_tasks()}
    # the execution record moved
    assert wiring.execution_store.load("t1") is None
    assert len(sorted((wiring.tmp_path / "executions" / "archive").glob("t1-*.json"))) == 1
    # the worker repository moved
    assert not (wiring.tmp_path / "workers" / "t1").exists()
    assert len(sorted((wiring.tmp_path / "quarantine").glob("t1-*"))) == 1
    # and the durable marker is spent, because all three agree
    assert wiring.orch._split_intents.pending() == ()
    assert wiring.orch.state.phase == Phase.READY.value
    # and the plan itself was not refused
    codes = [r.get("code") for r in records(wiring, "policy_denied")]
    assert not [code for code in codes if "split" in (code or "")], codes


def test_a_correctly_ordered_plan_leaves_every_brief_byte_identical(tmp_path):
    """The negative, through the whole loop rather than through the analysis
    alone. A check that appended something to every plan would make the warning
    worthless the week it shipped."""
    wiring = at_the_ceiling_with_a_python_tree(tmp_path)

    dispatch(wiring, plan(CORRECTED_SPECS))

    for child_id in ("t1-a", "t1-b"):
        assert (
            wiring.registry.get(child_id).description
            == "one independently reviewable piece"
        )
    outbox = wiring.orch.state.outbox or ""
    assert "DECOMPOSITION APPLIED" in outbox
    assert "SPLIT-ORDER" not in outbox
    data = split_records(wiring)[0]
    assert data["split_order_ran"] is True
    assert data["split_order_edges"] == []


def test_a_check_that_could_not_run_still_applies_the_plan_and_records_it(tmp_path, monkeypatch):
    """THE fail-open case at the level that matters: the loop's own acceptance.
    Both halves asserted — the plan lands in full, and the record says the
    check did not look. A test asserting only the first would pass on a check
    that had silently switched itself off.

    The did-not-run notice reaches ALL THREE destinations a warning reaches,
    including every successor's brief. That is the half a first cut dropped: it
    appended to the briefs only when the report was `flagged`, so a `ran=False`
    — which by construction names no edge — vanished from every brief while the
    transcript and the reviewer's report both carried it. The agent it matters
    to is the one whose part cannot succeed inside its own approved paths, and
    "nobody checked the order of this plan" is what stops it spending an attempt
    budget concluding the fault must be its own.

    The fault raised is deliberately NOT one of the anticipated ones: it goes
    through `split_order_warnings`'s bare `except Exception`, which is the arm
    that has to hold if an advisory read of a plan is never to be able to park
    a split. `OSError` — the anticipated arm — is graded a few tests above,
    where the round costs nothing."""
    wiring = at_the_ceiling_with_a_python_tree(tmp_path)

    def boom(root):
        raise ZeroDivisionError("nonsense from deep inside the walk")

    monkeypatch.setattr(validation, "build_import_graph", boom)
    dispatch(wiring, plan(INVERTED_SPECS))

    assert wiring.registry.get("t1").status == "retired"
    assert {"t1-a", "t1-b"} <= {t.id for t in wiring.registry.all_tasks()}
    assert wiring.orch._split_intents.pending() == ()
    data = split_records(wiring)[0]
    assert data["split_order_ran"] is False
    assert "ZeroDivisionError" in data["split_order_not_run_reason"]
    assert "nonsense from deep inside the walk" in data["split_order_not_run_reason"]
    assert data["split_order_edges"] == []
    outbox = wiring.orch.state.outbox or ""
    assert "SPLIT-ORDER CHECK DID NOT RUN" in outbox

    # and into every brief, read back from `tasks.json` as well as from memory:
    # the child is dispatched by a LATER loop out of the persisted registry, so
    # a notice that lived only in this process is no notice at all. The REASON
    # is asserted with the header, because "did not run" without "why" is the
    # panel nobody can act on.
    reloaded = wiring.task_store.load()
    for child_id in ("t1-a", "t1-b"):
        brief = wiring.registry.get(child_id).description
        assert brief.startswith("one independently reviewable piece")
        assert "SPLIT-ORDER CHECK DID NOT RUN" in brief
        assert "ZeroDivisionError" in brief
        assert "accepted unchanged" in brief
        assert reloaded.get(child_id).description == brief
        # The SAME rendered sentence in both places, not two spellings of it.
        # The reviewer re-orders from the report and the agent works from the
        # brief; a check that told the two different things would be worse than
        # one that said nothing.
        appended = brief.removeprefix("one independently reviewable piece").strip()
        assert appended and outbox.endswith(appended)


def test_a_loop_that_cannot_name_a_checkout_answers_did_not_run(tmp_path):
    """`self._git` is an injected collaborator, so the attribute access itself
    can fail in a mis-wired loop or a test double. The worst outcome allowed is
    a did-not-run report — never an exception escaping into the acceptance."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])

    class NoRoot:
        @property
        def repo_root(self):
            raise AttributeError("no checkout on this gateway")

    wiring.orch._git = NoRoot()
    report = wiring.orch._split_order_advisory(())

    assert report.ran is False
    assert report.flagged is False
    assert "could not resolve a checkout" in report.not_run_reason
    assert "AttributeError" in report.not_run_reason


def test_a_split_over_a_checkout_with_no_python_at_all_is_silent(tmp_path):
    """Every pre-existing split test runs over exactly this shape — a template
    repository holding one README. The advisory must be inert there, or it
    would have rewritten forty briefs the day it shipped."""
    wiring = first_round(tmp_path, tasks=[ready_task("t1")])
    ask_at_ceiling(wiring)

    dispatch(wiring, plan([spec("t1-a"), spec("t1-b", deps=("t1-a",))]))

    assert wiring.registry.get("t1-a").description == "one independently reviewable piece"
    assert "SPLIT-ORDER" not in (wiring.orch.state.outbox or "")
    data = split_records(wiring)[0]
    assert data["split_order_ran"] is True
    assert data["split_order_edges"] == []
    assert data["split_order_opaque_files"] == 0


def test_the_record_is_json_serialisable(tmp_path):
    """The transcript is JSONL; a field that cannot be encoded loses the whole
    event, which is where the record of this check lives."""
    wiring = at_the_ceiling_with_a_python_tree(tmp_path)
    dispatch(wiring, plan(INVERTED_SPECS))

    line = json.dumps(split_records(wiring)[0])

    assert "split_order_edges" in line


def test_never_looked_and_looked_and_found_nothing_are_told_apart(tmp_path):
    """The two states that leave an IDENTICAL edge list must be distinguishable
    from the record alone. This is the assertion the whole fail-open design
    rests on: without it, "no warnings" means nothing."""
    never_looked = split_order_warnings(tmp_path / "gone", (Part("p-a"), Part("p-b")))
    assert never_looked.ran is False
    assert never_looked.edges == ()
    assert never_looked.describe() != ""

    empty_tree = write_tree(tmp_path / "empty", {"README.md": "hi\n"})
    found_nothing = split_order_warnings(
        empty_tree, (Part("p-a"), Part("p-b", depends_on=("p-a",)))
    )
    assert found_nothing.ran is True
    assert found_nothing.edges == ()
    assert found_nothing.describe() == ""
