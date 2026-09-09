"""Audit executor end-to-end with fake agents + stubbed validation commands:
domain fan-out, raw-report persistence, reconciliation, Markdown-only report,
task proposal, revise-of-audit, audit-only policy, failure honesty."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from gitrepo import make_repo_from_template

from autoloop.audit.agents import AgentResult
from autoloop.audit.executor import AuditExecutor
from autoloop.audit.markdown import MarkdownPolicy
from autoloop.blockers import PLANNING_SOURCE_CONFLICT, BlockerStore
from autoloop.cli import _planning_sources
from autoloop.contract import Decision, Directive
from autoloop.git_gateway import GitGateway
from autoloop.inbox import attach_planning_sources
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import TaskRegistry


def run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    make_repo_from_template(
        root,
        branch="main",
        # `a.py` is TRACKED deliberately (ctx-06): every finding below cites
        # `a.py:12`, and a citation to a path this checkout does not have is
        # refused rather than believed — so a fixture without the file would make
        # every end-to-end run here assert on an empty proposal for a reason that
        # has nothing to do with what it is testing.
        files=(("README.md", "hi"), ("a.py", "x = 1\n")),
        email="t@e.c",
        name="T",
    )
    return root


def good_findings(fid="f1", category="defect"):
    return json.dumps(
        {
            "findings": [
                {
                    "id": fid,
                    "category": category,
                    "severity": "high",
                    "confidence": "confirmed",
                    "affected_files": ["a.py"],
                    "symbols": [],
                    # A LOCATION, which the schema has always asked `evidence`
                    # for ("file:line references to what you saw"). ctx-06 makes
                    # it load-bearing: a finding citing nowhere a reviewer can
                    # open is refused and becomes no task, so a fixture reading
                    # "seen" would make this whole end-to-end run assert on an
                    # empty proposal. `test_audit_taskgen` owns the uncited case.
                    "evidence": "a.py:12 seen",
                    "impact": "bad",
                    "proposed_action": "fix the thing",
                    "dependencies": [],
                    "acceptance_criteria": ["works"],
                    "validation_commands": ["ruff check ."],
                    "safe_to_parallelize": True,
                }
            ]
        }
    )


class FakeRunner:
    def __init__(self, outputs=None, fail_domains=()):
        self.outputs = outputs or {}
        self.fail_domains = set(fail_domains)
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        if spec.domain in self.fail_domains:
            return AgentResult(
                domain=spec.domain, raw_text="", returncode=1,
                duration_seconds=0.1, command=("claude",), error="boom",
            )
        text = self.outputs.get(spec.domain, json.dumps({"findings": []}))
        return AgentResult(
            domain=spec.domain, raw_text=text, returncode=0,
            duration_seconds=0.1, command=("claude",),
        )


def ok_command(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def planning_config(tmp_path):
    """The three fields `cli._planning_sources` reads, and nothing else.

    A stand-in rather than a real `AutoloopConfig` because building one needs a
    file and a whole deployment; what is being exercised is cli's own wiring
    function, which is the point — an end-to-end test that hand-rolled its own
    seam would pass on wiring production does not have.
    """
    return SimpleNamespace(
        workers_root=tmp_path / "workers",
        state_dir=tmp_path / "state",
        blockers_dir=tmp_path / "blockers",
    )


def build_executor(
    repo, tmp_path, runner=None, validation=(("ruff", "check", "."),), registry=None
):
    git = GitGateway(repo, PolicyEngine(PolicyConfig()))
    registry = registry if registry is not None else TaskRegistry()
    # ctx-06: the planning seam, attached through `cli`'s own wiring function and
    # onto the object `AuditExecutor` hands `generate_tasks`. This is the whole
    # production route — a store to record a conflict in, a tree to check a cited
    # location against, and the operator's drafts to compare with.
    attach_planning_sources(registry, _planning_sources(planning_config(tmp_path), repo))
    return AuditExecutor(
        git=git,
        agent_runner=runner or FakeRunner(),
        markdown=MarkdownPolicy(repo),
        registry=registry,
        run_dir_base=tmp_path / "runs",
        validation_commands=validation,
        max_parallel_agents=2,
        command_runner=ok_command,
    )


def audit_directive(scope=None):
    return Directive(decision=Decision.AUDIT, reason="r", scope=scope)


def test_full_audit_run(repo, tmp_path):
    runner = FakeRunner(outputs={"security_paths": good_findings("sec-1", "security")})
    executor = build_executor(repo, tmp_path, runner)
    outcome = executor.execute(audit_directive(scope="look at uploads"), None)
    assert outcome.status == "ok"
    # all six default domains fanned out, scope threaded into prompts
    assert len(runner.specs) == 6
    assert all("look at uploads" in s.prompt for s in runner.specs)
    assert all("READ-ONLY" in s.prompt for s in runner.specs)
    # one accepted finding -> one proposed task, in the report
    assert "1 accepted findings" in outcome.summary
    assert "au-001" in outcome.details
    assert "fix the thing" in outcome.details
    # the ONLY repo file written is the dated report
    written = [p for p in (repo / "docs").iterdir()]
    assert len(written) == 1 and written[0].name.startswith("AUDIT_")
    assert outcome.validation.startswith("ruff check .: PASS")


def test_raw_reports_persisted_separately(repo, tmp_path):
    runner = FakeRunner(outputs={"docs_drift": good_findings("d1", "doc_drift")})
    executor = build_executor(repo, tmp_path, runner)
    executor.execute(audit_directive(), None)
    [run_dir] = (tmp_path / "runs").iterdir()
    raw = run_dir / "raw"
    assert (raw / "docs_drift.txt").read_text() == good_findings("d1", "doc_drift")
    assert (raw / "docs_drift.meta.json").exists()
    assert (run_dir / "reconciled.json").exists()
    proposed = json.loads((run_dir / "proposed_tasks.json").read_text())
    assert proposed[0]["id"] == "au-001"


def test_agent_failure_reported_as_incomplete(repo, tmp_path):
    runner = FakeRunner(fail_domains={"db_migrations"})
    executor = build_executor(repo, tmp_path, runner)
    outcome = executor.execute(audit_directive(), None)
    assert outcome.status == "error"
    assert "COVERAGE INCOMPLETE" in outcome.summary
    assert "db_migrations" in outcome.details


def test_revise_of_audit_threads_feedback(repo, tmp_path):
    runner = FakeRunner()
    executor = build_executor(repo, tmp_path, runner)
    directive = Directive(
        decision=Decision.REVISE, reason="r", task_id="audit", feedback="check migration 036"
    )
    outcome = executor.execute(directive, None)
    assert outcome.status == "ok"
    assert all("check migration 036" in s.prompt for s in runner.specs)
    assert "check migration 036" in outcome.details


def test_non_audit_decisions_refused(repo, tmp_path):
    executor = build_executor(repo, tmp_path)
    directive = Directive(decision=Decision.IMPLEMENT, reason="r", task_id="t1")
    outcome = executor.execute(directive, None)
    assert outcome.status == "error"
    assert "supports only" in outcome.summary


def test_unsafe_validation_command_refused(repo, tmp_path):
    executor = build_executor(repo, tmp_path, validation=(("rm", "-rf", "/"),))
    outcome = executor.execute(audit_directive(), None)
    assert "FAIL" in outcome.validation
    assert "not a safe validation binary" in outcome.details


# ---- model allocation (operator Decision 2) --------------------------------


def test_domain_allocation_is_two_haiku_inventory_then_four_sonnet():
    from autoloop.audit.executor import DEFAULT_DOMAINS

    assert len(DEFAULT_DOMAINS) == 6
    slugs = [d[0] for d in DEFAULT_DOMAINS]
    models = [d[3] for d in DEFAULT_DOMAINS]
    # Order is wave order: wave 1 = first three (max_parallel_agents=3).
    assert slugs[:3] == ["docs_drift", "tests_ci", "repo_structure"]
    assert models[:3] == ["haiku", "haiku", "sonnet"]
    assert models[3:] == ["sonnet", "sonnet", "sonnet"]
    # Mechanical inventory never runs on an expensive model, and the lead's
    # model is never delegated to.
    assert dict(zip(slugs, models))["docs_drift"] == "haiku"
    assert dict(zip(slugs, models))["tests_ci"] == "haiku"
    assert "opus" not in models
    assert "fable" not in models


def test_executor_routes_each_domain_to_its_model(repo, tmp_path):
    runner = FakeRunner()
    build_executor(repo, tmp_path, runner).execute(audit_directive(), None)
    routed = {spec.domain: spec.model for spec in runner.specs}
    assert routed == {
        "docs_drift": "haiku",
        "tests_ci": "haiku",
        "repo_structure": "sonnet",
        "security_paths": "sonnet",
        "db_migrations": "sonnet",
        "ingestion_pipeline": "sonnet",
    }


def test_parallelism_is_capped_at_the_configured_worker_count(repo, tmp_path):
    """Six domains, cap of three: never more than three agents in flight."""
    import threading

    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    gate = threading.Barrier(3, timeout=5)

    class CountingRunner(FakeRunner):
        def run(self, spec):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            try:
                # Force three to be genuinely concurrent, so a serial
                # implementation would deadlock the barrier instead of passing.
                gate.wait()
                return super().run(spec)
            finally:
                with lock:
                    live["now"] -= 1

    executor = AuditExecutor(
        git=GitGateway(repo, PolicyEngine(PolicyConfig())),
        agent_runner=CountingRunner(),
        markdown=MarkdownPolicy(repo),
        registry=TaskRegistry(),
        run_dir_base=tmp_path / "runs",
        validation_commands=(),
        max_parallel_agents=3,
        command_runner=ok_command,
    )
    executor.execute(audit_directive(), None)
    assert live["peak"] == 3  # exactly the cap, excluding the lead


def test_shipped_config_caps_workers_at_three():
    from autoloop.config import AuditConfig

    assert AuditConfig().max_parallel_agents == 3


# ---- coverage honesty (the defect ChatGPT caught) --------------------------


def test_unusable_agent_output_is_reported_as_a_coverage_failure(repo, tmp_path):
    """The live regression: security_paths returned truncated JSON, contributed
    zero findings, and the report still claimed '0 agent failures'."""
    truncated = '{"findings": [{"id": "sec-01", "evidence": "unterminated'
    runner = FakeRunner(outputs={"security_paths": truncated})
    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)
    assert outcome.status == "error"
    assert "COVERAGE INCOMPLETE" in outcome.summary
    assert "security_paths" in outcome.details
    assert "output unusable" in outcome.details


def test_report_shows_per_domain_coverage_and_names_the_missing_domain(repo, tmp_path):
    truncated = '{"findings": [{"id": "sec-01"'
    runner = FakeRunner(outputs={"security_paths": truncated})
    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)
    assert "## Domain coverage — 5/6 domains reported usable output" in outcome.details
    assert "| `security_paths` | **NO** | 0 |" in outcome.details
    assert "treat their areas as unaudited, not as clean" in outcome.details


def test_full_coverage_reports_six_of_six(repo, tmp_path):
    outcome = build_executor(repo, tmp_path, FakeRunner()).execute(audit_directive(), None)
    assert "## Domain coverage — 6/6 domains reported usable output" in outcome.details
    assert "COVERAGE INCOMPLETE" not in outcome.details
    assert outcome.status == "ok"


def test_one_bad_finding_does_not_mark_a_domain_uncovered(repo, tmp_path):
    """A single rejected item is not a coverage gap — the domain still reported."""
    mixed = json.dumps(
        {"findings": [json.loads(good_findings())["findings"][0], {"id": "broken"}]}
    )
    runner = FakeRunner(outputs={"db_migrations": mixed})
    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)
    assert outcome.status == "ok"
    assert "## Domain coverage — 6/6 domains reported usable output" in outcome.details


def test_report_details_are_capped_for_the_review_payload():
    """A ~70 kB report inlined into `details` produced 104k/113k/122k-character
    messages — most of everything this loop ever sent, and the load that wedged
    a conversation into accepting messages and never replying."""
    from autoloop.audit.executor import MAX_REPORT_DETAILS_CHARS, cap_report_details

    report = "COVERAGE TABLE\n" + ("finding line\n" * 8000)
    assert len(report) > 70_000
    capped = cap_report_details(report, "docs/AUDIT_2026-07-31.md")

    assert len(capped) < MAX_REPORT_DETAILS_CHARS + 500
    assert capped.startswith("COVERAGE TABLE"), "the head is what a reviewer needs"
    assert "EXCERPT" in capped, "truncation must be announced, never silent"
    assert "docs/AUDIT_2026-07-31.md" in capped, "must name where the full text is"


def test_a_short_report_is_not_truncated():
    from autoloop.audit.executor import cap_report_details

    assert cap_report_details("a short report", "p.md") == "a short report"


# ---- scope semantics (tests_ci:arch-01) ------------------------------------

#: The scope string from the live run that produced the finding: it describes
#: the ENTIRE multi-domain audit process, not a focus within one domain.
ORCHESTRATION_SCOPE = (
    "Run parallel read-only domain reviews, apply the Opus/Sonnet/Haiku task "
    "routing, and produce one dated Markdown report proposing the task graph."
)


def test_scope_is_framed_as_narrowing_the_agents_own_domain():
    """A scope may only narrow this one agent's domain. The prompt has to say
    so — and say it BEFORE the reviewer's words, which are threaded verbatim
    and so cannot themselves be sanitised."""
    from autoloop.audit.executor import _agent_prompt

    prompt = _agent_prompt(
        "Security, path handling and data integrity",
        "charter text",
        ORCHESTRATION_SCOPE,
        None,
    )
    # The reviewer's words still reach the agent, unaltered.
    assert ORCHESTRATION_SCOPE in prompt
    # ...but bounded, and bounded first: framing before data.
    assert "NARROWS your own domain" in prompt
    assert "does not grant" in prompt
    assert "permission to delegate" in prompt
    assert prompt.index("NARROWS your own domain") < prompt.index(ORCHESTRATION_SCOPE)
    # The domain is named inside the framing, so "narrows" has a referent.
    assert "within Security, path handling and data integrity" in prompt
    # None of this displaces the standing ground rules.
    assert "READ-ONLY" in prompt
    assert "Stay in your domain." in prompt


def test_no_scope_means_no_scope_framing():
    from autoloop.audit.executor import _agent_prompt

    prompt = _agent_prompt("Documentation drift", "charter text", None, None)
    assert "reviewer scope" not in prompt
    assert "Stay in your domain." in prompt


def test_orchestration_flavoured_scope_leaves_every_domain_single_domain(repo, tmp_path):
    """The regression: an orchestration-shaped scope reached six single-domain
    agents as if it were their remit. Each prompt must still be about ITS own
    domain, and must still refuse authority the scope never carried."""
    runner = FakeRunner()
    executor = build_executor(repo, tmp_path, runner)
    outcome = executor.execute(audit_directive(scope=ORCHESTRATION_SCOPE), None)
    assert outcome.status == "ok"
    assert len(runner.specs) == 6

    titles = {spec.domain: spec.title for spec in runner.specs}
    for spec in runner.specs:
        assert ORCHESTRATION_SCOPE in spec.prompt, "verbatim threading is deliberate"
        assert f"Your domain: {titles[spec.domain]}." in spec.prompt
        assert f"within {titles[spec.domain]}" in spec.prompt
        assert "NARROWS your own domain" in spec.prompt
        assert "does not grant" in spec.prompt
        assert "responsible for other domains" in spec.prompt
        assert "Stay in your domain." in spec.prompt
    # Six prompts, six different domains — no agent is told it owns the run.
    assert len({spec.title for spec in runner.specs}) == 6


def test_revision_feedback_carries_the_same_containment():
    from autoloop.audit.executor import _agent_prompt

    prompt = _agent_prompt("Documentation drift", "charter text", None, "re-check 036")
    assert "re-check 036" in prompt
    assert "grants no additional authority" in prompt
    assert "within Documentation drift" in prompt


def test_audit_validation_is_normalised_like_every_other_validation_run():
    """The audit validates the WHOLE repository to prove its one markdown file
    broke nothing, so it pays the suite's full cost on every run.

    It used to run the CONFIGURED argv raw — `_run_validation` called the command
    runner directly rather than going through `effective_validation_command` — so
    a pytest command got no `-n auto`. Measured 2026-08-28: an audit sat on the
    full suite SERIALLY for over fifteen minutes, where eight workers finish it
    in about seven. Nothing failed and nothing said so; it was simply slow.
    """
    from autoloop.validation import effective_validation_command

    raw = ("python3", "-m", "pytest", "autoloop/tests", "-q")
    assert effective_validation_command(raw) == (
        "python3", "-m", "pytest", "-n", "auto", "-p", "no:cacheprovider",
        "autoloop/tests", "-q",
    )


def test_an_audit_unit_id_is_not_in_the_roadmap_namespace():
    """`autoaudit-0007`, not `audit-0007`.

    A roadmap task may legitimately be called `audit-0003`, and units are minted
    from the loop iteration, which RESETS each session — so a restarted loop
    re-mints ids it has used before. On this repository a completed roadmap task
    `audit-0001` and a fresh audit unit of the same name appeared as one id in
    the merge backlog, the dashboard and the transcript.

    Both spellings are still RECOGNISED: worker repositories, quarantine entries
    and archived records written before the rename carry the old one, and a
    shipped record is never rewritten.
    """
    from autoloop.contract import AUDIT_TASK_ID, AUDIT_UNIT_PREFIX, is_audit_unit

    assert AUDIT_UNIT_PREFIX == "autoaudit-"
    assert not AUDIT_UNIT_PREFIX.startswith(AUDIT_TASK_ID)
    assert is_audit_unit("autoaudit-0001")
    assert is_audit_unit("audit-0001"), "a unit minted before the rename still counts"
    assert not is_audit_unit("t1")
    assert not is_audit_unit("brw-19c")


# ---- planning discipline on the PRODUCTION path (ctx-06) -------------------
#
# `AuditExecutor.execute` is the only caller of `generate_tasks` that ships, so
# the discipline is worth exactly what it is worth THERE. These two drive the
# real executor rather than calling the generator directly: a guard that holds
# in `test_audit_taskgen.py` and is bypassed by the wiring is a guard nobody has.


def audited_registry(*tasks):
    return TaskRegistry(list(tasks))


def written_report(repo):
    [report] = [p for p in (repo / "docs").iterdir() if p.name.startswith("AUDIT_")]
    return report.read_text(encoding="utf-8")


def test_the_audit_path_stops_rather_than_proposing_a_task_over_a_conflict(repo, tmp_path):
    """A real audit run whose finding disagrees with an accepted task about which
    files the work touches. The run must not quietly propose the task anyway, and
    the operator's REPORT — not an object in memory — must name both sources."""
    from autoloop.tasks import Task

    runner = FakeRunner(outputs={"security_paths": good_findings("f1", "security")})
    registry = audited_registry(
        Task(
            id="already",
            title="t",
            description="covers security_paths:f1",
            approved_paths=("b.py",),          # the finding needs a.py
        )
    )
    outcome = build_executor(repo, tmp_path, runner, registry=registry).execute(
        audit_directive(), None
    )

    # NOTHING was proposed, and the artifact a `plan` decision is adopted from is
    # empty rather than quietly holding a task nobody reconciled.
    [run_dir] = (tmp_path / "runs").iterdir()
    assert json.loads((run_dir / "proposed_tasks.json").read_text()) == []
    assert "0 tasks proposed" in outcome.summary

    report = written_report(repo)
    assert "generation stopped" in report
    assert "accepted_decision" in report and "repository" in report
    assert "a.py" in report and "b.py" in report
    assert "No winner was chosen" in report
    # AND THE RECORD EXISTS, on disk, where `python -m autoloop blockers` reads
    # it. This is what the wiring buys: before it, the audit path had no store
    # and the report could only say there was nothing to go and answer.
    [blocker] = BlockerStore(tmp_path / "blockers").open_blockers()
    assert blocker.code == PLANNING_SOURCE_CONFLICT
    assert "a.py" in blocker.detail and "b.py" in blocker.detail
    assert "NO DURABLE RECORD EXISTS" not in report


def test_an_ordinary_audit_run_still_proposes_its_tasks(repo, tmp_path):
    """The control, without which the test above is satisfied by a generator that
    stopped on everything. Same run, same finding, a registry that says nothing
    about it: the task is proposed and no conflict is reported."""
    runner = FakeRunner(outputs={"security_paths": good_findings("f1", "security")})
    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)

    [run_dir] = (tmp_path / "runs").iterdir()
    assert [t["id"] for t in json.loads((run_dir / "proposed_tasks.json").read_text())] == [
        "au-001"
    ]
    assert "1 tasks proposed" in outcome.summary
    assert "generation stopped" not in written_report(repo)


def test_the_audit_path_records_a_non_scope_conflict_and_keeps_going(repo, tmp_path):
    """THE ACCEPTANCE CRITERION THAT NEEDS A REAL STORE, on the shipping path.

    Two sources that word one scope differently and mean the SAME files: an
    accepted task and the operator's own draft. Nothing about the work changes,
    so generation must not stop — and the disagreement must still be durable,
    because "recorded and continued" is only honest when the record exists.
    """
    from autoloop.inbox import DraftTask, IntakeDraft, intake_dir_for, render_draft
    from autoloop.tasks import Task

    config = planning_config(tmp_path)
    intake = intake_dir_for(config.workers_root, config.state_dir)
    intake.mkdir(parents=True, exist_ok=True)
    (intake / "idea.md").write_text(
        render_draft(
            IntakeDraft(
                slug="idea",
                idea="tidy up security_paths:f1",
                tasks=(
                    DraftTask(
                        id="t1", title="t",
                        # The same two files the accepted task names, written the
                        # other way round: different words, identical scope.
                        approved_paths=("b.py", "a.py"),
                    ),
                ),
            )
        ),
        encoding="utf-8",
    )
    registry = audited_registry(
        Task(
            id="already",
            title="t",
            description="covers security_paths:f1",
            approved_paths=("a.py", "b.py"),
        )
    )
    runner = FakeRunner(outputs={"security_paths": good_findings("f1", "security")})
    outcome = build_executor(repo, tmp_path, runner, registry=registry).execute(
        audit_directive(), None
    )

    # Generation CONTINUED.
    [run_dir] = (tmp_path / "runs").iterdir()
    assert [t["id"] for t in json.loads((run_dir / "proposed_tasks.json").read_text())] == [
        "au-001"
    ]
    assert "1 tasks proposed" in outcome.summary
    assert "generation stopped" not in written_report(repo)

    # And the conflict is DURABLE, naming both sources, with no winner.
    [blocker] = BlockerStore(tmp_path / "blockers").open_blockers()
    assert blocker.code == PLANNING_SOURCE_CONFLICT
    assert "accepted_decision: already" in blocker.detail
    assert "draft:idea:t1" in blocker.detail
    # The round that works the task is told, too.
    proposed = json.loads((run_dir / "proposed_tasks.json").read_text())
    assert "Recorded source conflict (no winner chosen)" in proposed[0]["description"]


def test_the_wiring_layer_supplies_every_planning_input(repo, tmp_path):
    """`cli._planning_sources` is the whole production half of ctx-06, so what it
    carries is asserted directly rather than inferred from a run that happened to
    behave."""
    from autoloop.cli import CONTEXT_TIER_UNWIRED_NOTE
    from autoloop.inbox import intake_dir_for, resolve_tree

    config = planning_config(tmp_path)
    sources = _planning_sources(config, repo)

    assert sources.blocker_store.directory == tmp_path / "blockers"
    assert CONTEXT_TIER_UNWIRED_NOTE in sources.notes

    # THE TREE IS LAZY, and this is what that buys: a file added after the loop
    # started is still verifiable. Read once at wiring time, a citation to it
    # would be refused for the rest of the process's life.
    (repo / "later.py").write_text("y = 2\n", encoding="utf-8")
    run_git(repo, "add", "later.py")
    tree = resolve_tree(sources.tree)
    assert tree.read and tree.holds("a.py") and tree.holds("later.py")

    # And the provider reads the operator's drafts from the intake directory
    # this deployment uses — the operator-request tier, wired, not represented.
    from autoloop.inbox import DraftTask, IntakeDraft, render_draft

    intake = intake_dir_for(config.workers_root, config.state_dir)
    intake.mkdir(parents=True, exist_ok=True)
    (intake / "idea.md").write_text(
        render_draft(
            IntakeDraft(
                slug="idea",
                idea="about security_paths:f1",
                tasks=(DraftTask(id="t1", title="t", approved_paths=("a.py",)),),
            )
        ),
        encoding="utf-8",
    )

    class _Finding:
        qualified_id = "security_paths:f1"

    claims, notes = sources.provider([_Finding()])
    [claim] = claims
    assert claim.author == "draft:idea:t1" and claim.paths == ("a.py",)
    assert notes == ()


def test_the_audit_path_says_which_tier_it_did_not_compare(repo, tmp_path):
    """A tier with no producer must not read as a tier that agreed. The context
    records have no index anywhere in this loop, and the report says so rather
    than leaving a reviewer to infer silence meant assent."""
    runner = FakeRunner(outputs={"security_paths": good_findings("f1", "security")})
    build_executor(repo, tmp_path, runner).execute(audit_directive(), None)

    assert "no context record index is wired" in written_report(repo)
