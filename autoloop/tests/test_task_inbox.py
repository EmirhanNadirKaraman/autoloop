"""Operator task inbox + priority ordering.

The point of the inbox is that it is safe to write at ANY moment, including
while a write-capable agent is running. That safety rests on one property —
it lives outside the checkout, so the escape detector never sees it — and the
first test below pins exactly that.
"""

from __future__ import annotations

import json

import pytest

from autoloop.inbox import InboxError, TaskInbox, inbox_dir_for
from autoloop.tasks import Task, TaskRegistry


def test_the_inbox_lives_outside_the_checkout(tmp_path):
    """The property the whole design depends on. `escape_detector` snapshots
    tracked + untracked + IGNORED paths, so anything under the repo — including
    the gitignored `.autoloop/` — is inside the before/after comparison taken
    around every agent call. An operator write landing there mid-execute would
    park the loop LOOP-FATAL. Placing the inbox beside `workers_root`, which is
    already required to be external, is what makes mid-run submission safe."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    workers_root = tmp_path / "outside" / "workers"

    inbox = inbox_dir_for(workers_root, repo / ".autoloop")

    with pytest.raises(ValueError):
        inbox.resolve().relative_to(repo.resolve())


def test_submit_then_drain_round_trip(tmp_path):
    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit({"id": "a-1", "title": "T", "description": "D", "priority": 2})
    inbox.submit({"id": "a-2", "title": "T2", "description": "D2"})

    specs, problems = inbox.drain()
    assert problems == []
    assert [s["id"] for s in specs] == ["a-1", "a-2"], "submission order"
    assert specs[0]["priority"] == 2
    # Drained means gone — a second drain must not replay the same requests.
    assert inbox.drain() == ([], [])


def test_submit_refuses_a_malformed_request(tmp_path):
    inbox = TaskInbox(tmp_path / "inbox")
    with pytest.raises(InboxError, match="missing required"):
        inbox.submit({"id": "a-1", "title": "T"})
    with pytest.raises(InboxError, match="unknown field"):
        inbox.submit({"id": "a", "title": "T", "description": "D", "urgency": "high"})
    with pytest.raises(InboxError, match="priority must be an integer"):
        inbox.submit({"id": "a", "title": "T", "description": "D", "priority": "high"})
    assert inbox.pending() == [], "nothing malformed should reach the queue"


def test_an_unparseable_request_is_quarantined_not_replayed_forever(tmp_path):
    """One typo must never stop a running loop, and must not re-fail on every
    drain. Moved aside rather than deleted — it is what the operator wrote."""
    inbox = TaskInbox(tmp_path / "inbox")
    inbox.directory.mkdir(parents=True)
    (inbox.directory / "20260801T000000Z-1-1.json").write_text("{not json", encoding="utf-8")

    specs, problems = inbox.drain()
    assert specs == []
    assert len(problems) == 1
    assert inbox.drain() == ([], []), "must not re-fail forever"
    assert list((inbox.directory / "rejected").glob("*.json")), "evidence preserved"


def test_submit_is_atomic(tmp_path):
    """A drain racing a submit must never see a half-written file."""
    inbox = TaskInbox(tmp_path / "inbox")
    path = inbox.submit({"id": "a-1", "title": "T", "description": "D"})
    assert json.loads(path.read_text())["id"] == "a-1"
    assert not list(inbox.directory.glob("*.tmp")), "no temp file left behind"


# ---- priority ordering -------------------------------------------------------


def test_next_ready_prefers_the_lower_priority_number():
    registry = TaskRegistry()
    registry.add_many([
        Task(id="later", title="L", description="d", priority=5),
        Task(id="urgent", title="U", description="d", priority=1),
        Task(id="default", title="D", description="d"),  # 100
    ])
    assert registry.next_ready().id == "urgent"


def test_a_task_added_later_can_overtake_one_already_queued():
    """The reason ordering changed from insertion order: otherwise an operator
    cannot steer a running loop — a task added mid-run could never be picked
    before the ones already queued, however urgent."""
    registry = TaskRegistry()
    registry.add_many([Task(id="first", title="F", description="d", priority=50)])
    assert registry.next_ready().id == "first"

    registry.add_many([Task(id="second", title="S", description="d", priority=1)])
    assert registry.next_ready().id == "second"


def test_equal_priorities_break_on_id_not_dict_order():
    registry = TaskRegistry()
    registry.add_many([
        Task(id="zzz", title="Z", description="d", priority=7),
        Task(id="aaa", title="A", description="d", priority=7),
    ])
    assert registry.next_ready().id == "aaa"


def test_priority_survives_a_persistence_round_trip(tmp_path):
    from autoloop.tasks import TaskStore

    store = TaskStore(tmp_path / "tasks.json")
    registry = TaskRegistry()
    registry.add_many([Task(id="p", title="P", description="d", priority=3)])
    store.save(registry)
    assert store.load().get("p").priority == 3


def test_an_old_tasks_file_without_priority_still_loads(tmp_path):
    """Backward compatibility: a roadmap written before the field existed must
    keep working, defaulting to last place rather than jumping the queue."""
    from autoloop.tasks import TaskStore

    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "tasks": [{"id": "old", "title": "O", "description": "d", "status": "pending"}],
    }), encoding="utf-8")
    assert TaskStore(path).load().get("old").priority == 100


# ---- the shared merge (one implementation, two callers) ----------------------


def test_apply_requests_is_the_single_merge_used_by_both_callers():
    """`Orchestrator._drain_task_inbox` and `python -m autoloop drain-inbox`
    must apply a request identically. Two copies would drift, and a drift means
    the same request behaves differently depending on who applied it."""
    import inspect

    from autoloop import cli, orchestrator
    from autoloop.inbox import apply_requests

    assert "apply_requests(" in inspect.getsource(orchestrator.Orchestrator._drain_task_inbox)
    assert "apply_requests(" in inspect.getsource(cli._cmd_drain_inbox)
    assert callable(apply_requests)


def test_apply_requests_adds_reprioritises_and_refuses_in_one_pass():
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    registry.add_many([Task(id="existing", title="E", description="d", priority=9)])

    added, reprioritised, refused = apply_requests(registry, [
        {"id": "brand-new", "title": "N", "description": "d", "priority": 2},
        {"kind": "priority", "id": "existing", "priority": 1},
        {"id": "existing", "title": "dupe", "description": "d"},      # duplicate id
        {"kind": "priority", "id": "ghost", "priority": 1},           # unknown task
    ])

    assert len(added) == 1 and "brand-new" in added[0]
    assert len(reprioritised) == 1 and "existing -> 1" in reprioritised[0]
    assert len(refused) == 2, refused
    # The good ones landed despite the bad ones queued alongside.
    assert registry.get("brand-new").priority == 2
    assert registry.get("existing").priority == 1
    assert registry.get("existing").title == "E", "the original is untouched"


def test_a_refused_batch_never_raises():
    """One typo must not discard the good requests queued behind it."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, reprioritised, refused = apply_requests(registry, [
        {"id": "", "title": "", "description": ""},
        {"id": "ok", "title": "T", "description": "d"},
    ])
    assert [a.split(" ")[0] for a in added] == ["ok"]
    assert len(refused) == 1


# ---- the mutation vocabulary -------------------------------------------------
#
# `task` + `priority` became `task` + six mutations. What keeps that from being
# the "general edit-a-task request" the priority-only design refused: the two
# authorities are split by question and each has ONE implementation (shape is
# `check_request_shape`, run at both gates; content is the registry), nothing a
# dispatch is currently reading can be edited, and blocking has a reverse.
# These pin all three.


def test_every_mutation_kind_reaches_its_registry_mutator(tmp_path):
    """One round trip per kind, through the real inbox: submit, drain, apply.
    A kind that is in the vocabulary but wired to nothing would otherwise look
    fine at submit and silently do nothing on merge."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("priority", "t", 1)
    inbox.submit_mutation("description", "t", "rewritten instructions")
    inbox.submit_mutation("approved_paths", "t", ["autoloop/inbox.py"])
    inbox.submit_mutation("depends_on", "t", ["dep"])
    inbox.submit_mutation("context_ids", "t", ["ctx-decision-01"])
    inbox.submit_mutation("block", "t", "waiting on the API key")
    inbox.submit_mutation("unblock", "t")

    registry = TaskRegistry()
    registry.add_many([Task(id="dep", title="D", description="d"),
                       Task(id="t", title="T", description="d")])
    specs, problems = inbox.drain()
    added, applied, refused = apply_requests(registry, specs)

    assert (problems, added, refused) == ([], [], [])
    assert len(applied) == 7, applied
    task = registry.get("t")
    assert task.priority == 1
    assert task.description == "rewritten instructions"
    assert task.approved_paths == ("autoloop/inbox.py",)
    assert task.depends_on == ("dep",)
    assert task.context_ids == ("ctx-decision-01",)
    assert task.status == "pending", "blocked then released"
    assert task.blocked_reason == ""


def test_a_mutation_request_carries_only_its_own_field(tmp_path):
    """The rule the priority branch has always had, now driven off
    `MUTATION_PAYLOAD` so a new kind cannot forget it. A request naming a field
    its kind ignores has not done what its author intended, so it is refused
    rather than dropped. This is the submit gate; the merge gate runs the same
    check — see
    `test_a_hand_written_mutation_carrying_a_foreign_field_is_refused_atomically`."""
    inbox = TaskInbox(tmp_path / "inbox")
    with pytest.raises(InboxError, match="carries only"):
        inbox.submit({"kind": "description", "id": "t", "description": "d",
                      "approved_paths": ["a.py"]})
    with pytest.raises(InboxError, match="carries only"):
        inbox.submit({"kind": "unblock", "id": "t", "reason": "because"})
    with pytest.raises(InboxError, match="needs the task 'id'"):
        inbox.submit({"kind": "block", "id": "  ", "reason": "because"})
    with pytest.raises(InboxError, match="needs 'approved_paths' as a list"):
        inbox.submit({"kind": "approved_paths", "id": "t", "approved_paths": "a.py"})
    assert inbox.pending() == [], "nothing malformed should reach the queue"


def test_submission_validates_shape_only_and_leaves_content_to_the_registry(tmp_path):
    """Registry-derived refusal reasons. A blank description and a path with a
    glob in it are both well-SHAPED, so they queue — and are then refused on
    merge in the registry's own words, by the same validators creation calls.
    A second rule set here would drift and start refusing what `add_many`
    accepts."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("description", "t", "   ")
    inbox.submit_mutation("approved_paths", "t", ["autoloop/*.py"])
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="original")])

    specs, _ = inbox.drain()
    _, applied, refused = apply_requests(registry, specs)

    assert applied == []
    assert len(refused) == 2, refused
    assert "non-empty description" in refused[0]
    assert "no globs" in refused[1]
    assert registry.get("t").description == "original", "nothing half-applied"


def test_a_mutation_cannot_strand_a_task_the_loop_is_running(tmp_path):
    """The refusal that makes the whole vocabulary safe to expose. All three
    content fields are what an already-started dispatch is judged against; the
    dependency case is the one with no way out at all, since the round then
    fails BOTH `mark_completed` and `release`."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("depends_on", "running", ["other"])
    inbox.submit_mutation("approved_paths", "running", ["autoloop/tasks.py"])
    inbox.submit_mutation("block", "running", "hold this")
    # ... while the one mutation that cannot strand anything still lands.
    inbox.submit_mutation("priority", "running", 1)

    registry = TaskRegistry()
    registry.add_many([Task(id="other", title="O", description="d"),
                       Task(id="running", title="R", description="d",
                            approved_paths=("autoloop/inbox.py",))])
    registry.mark_in_progress("running")

    specs, _ = inbox.drain()
    _, applied, refused = apply_requests(registry, specs)

    assert len(refused) == 3, refused
    assert all("in progress" in line for line in refused), refused
    assert applied == ["running -> 1"]
    task = registry.get("running")
    assert (task.depends_on, task.approved_paths, task.status) == (
        (), ("autoloop/inbox.py",), "in_progress",
    )


def test_an_inbox_request_cannot_empty_a_running_tasks_scope(tmp_path):
    """The same refusal as `test_a_mutation_cannot_strand_a_task_the_loop_is_running`,
    driven with the value that test cannot use: `[]`. A rewrite is
    stranding; an EMPTY scope is stranding plus un-authorized, since an empty
    `approved_paths` is what dispatch refuses outright — so the running round
    would end up judged against a scope it is no longer allowed to have.

    The control in the same batch is the other half of the rule: a non-empty
    edit against a task the loop is NOT running still lands, and lands in the
    `applied` bucket, which is what both drain sites gate `task_store.save()`
    on. A guard written as "refuse approved_paths from the inbox" would pass the
    refusal assertions here and fail this one."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("approved_paths", "running", [])
    inbox.submit_mutation("approved_paths", "queued", ["autoloop/tasks.py"])

    registry = TaskRegistry()
    registry.add_many([Task(id="running", title="R", description="d",
                            approved_paths=("autoloop/inbox.py",)),
                       Task(id="queued", title="Q", description="d",
                            approved_paths=("autoloop/inbox.py",))])
    registry.mark_in_progress("running")

    specs, _ = inbox.drain()
    _, applied, refused = apply_requests(registry, specs)

    assert len(refused) == 1, refused
    assert "in progress" in refused[0], refused[0]
    assert registry.get("running").approved_paths == ("autoloop/inbox.py",)
    assert applied == ["queued -> approved_paths: autoloop/tasks.py"]
    assert registry.get("queued").approved_paths == ("autoloop/tasks.py",)


# ---- context_ids through the inbox (ctx-04) ---------------------------------
#
# The kind is shaped exactly like `approved_paths` and carries none of its
# authority. These own both halves of that sentence: it behaves the same
# (creation, correction, refused in flight) and it moves no scope.


def test_a_creation_request_can_name_the_context_it_was_written_from(tmp_path):
    """The half `CREATION_FIELDS` exists to make true: a field the contract
    admits is a field the merge READS. `apply_requests`' creation branch builds
    the `Task` kwarg by kwarg, so a name added to that set and forgotten there
    would submit cleanly and then vanish — the exact silent drop the per-kind
    rule was written against."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit({"id": "t", "title": "T", "description": "d",
                  "approved_paths": ["autoloop/tasks.py"],
                  "context_ids": ["ctx-decision-01", "ctx-incident-02"]})

    registry = TaskRegistry()
    specs, _ = inbox.drain()
    added, _applied, refused = apply_requests(registry, specs)

    assert (len(added), refused) == (1, [])
    assert registry.get("t").context_ids == ("ctx-decision-01", "ctx-incident-02")
    assert registry.get("t").approved_paths == ("autoloop/tasks.py",)


def test_a_creation_request_naming_a_bare_string_is_refused_not_split(tmp_path):
    """The fail-open this route would otherwise have. `tuple("ctx01")` is five
    ids that `tasks._ID_RE` accepts one at a time, so a `tuple()` on the way
    past would make the mistake unreportable — the value is handed to the
    registry as it arrived and refused in the registry's own words."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, _applied, refused = apply_requests(registry, [
        {"id": "t", "title": "T", "description": "d", "context_ids": "ctx01"},
    ])

    assert added == []
    assert len(refused) == 1 and "context_ids" in refused[0], refused
    assert not registry.has("t"), "nothing half-created"


#: Values that are FALSY and are NOT a list of ids. Every one of them is
#: malformed, and every one of them was normalised away by the `or ()` these
#: two construction sites used to carry — a refusal turned into a silent "cites
#: no record" with the request still reporting the task created. The bare
#: string has its own test above; `""` is here because, being falsy, it took
#: the other route past the validator entirely.
FALSY_NOT_A_LIST = [0, False, {}, ""]
FALSY_IDS = ["zero", "false", "object", "empty-string"]


@pytest.mark.parametrize("bad", FALSY_NOT_A_LIST, ids=FALSY_IDS)
def test_a_falsy_creation_context_ids_is_refused_rather_than_emptied(bad):
    """The fail-open a falsy-test normalisation leaves behind: it deletes the
    provenance the field exists to record and reports success while doing it,
    so the operator has no line to read and the task looks fully described.
    Handed to the registry as it arrived, each one is refused in the registry's
    own words and nothing is created."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, _applied, refused = apply_requests(registry, [
        {"id": "t", "title": "T", "description": "d", "context_ids": bad},
    ])

    assert added == []
    assert len(refused) == 1 and "context_ids" in refused[0], refused
    assert not registry.has("t"), "nothing half-created"


def test_creation_still_accepts_the_three_ways_of_citing_nothing():
    """The other half of the rule above, and what keeps it from being "refuse
    everything falsy": `[]` is the documented way to say "this task cites no
    record", a MISSING key is every request written before the field existed,
    and `null` is the hand-edited spelling of the same. All three create the
    task, and all three land as a TUPLE — every reader iterates and joins this
    field without a `None` check."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, _applied, refused = apply_requests(registry, [
        {"id": "empty", "title": "T", "description": "d", "context_ids": []},
        {"id": "absent", "title": "T", "description": "d"},
        {"id": "nulled", "title": "T", "description": "d", "context_ids": None},
    ])

    assert (len(added), refused) == (3, [])
    for task_id in ("empty", "absent", "nulled"):
        assert registry.get(task_id).context_ids == (), task_id
        assert isinstance(registry.get(task_id).context_ids, tuple), task_id


def test_an_inbox_request_can_correct_an_existing_tasks_references(tmp_path):
    """The reason this is a MUTATION kind and not creation-only: the reference
    that turns out to be wrong is exactly the one written at planning time.
    REPLACES, so a correction can drop an id as well as add one."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("context_ids", "t", ["ctx-decision-01", "ctx-incident-02"])
    inbox.submit_mutation("context_ids", "t", ["ctx-decision-03"])

    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d",
                            approved_paths=("autoloop/inbox.py",))])
    specs, _ = inbox.drain()
    _, applied, refused = apply_requests(registry, specs)

    assert refused == []
    assert len(applied) == 2 and "no scope change" in applied[-1], applied
    assert registry.get("t").context_ids == ("ctx-decision-03",), "last write wins"


def test_correcting_references_moves_no_authorized_path(tmp_path):
    """THE claim, at the inbox gate. The request shape is
    `KIND_APPROVED_PATHS`' shape, so the thing that must differ is the effect:
    the scope is byte-identical before and after, and the ids it names are
    still unauthorized paths."""
    from autoloop.inbox import apply_requests
    from autoloop.tasks import effective_approved_paths, unauthorized_paths

    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d",
                            approved_paths=("autoloop/inbox.py",))])
    before = effective_approved_paths(registry.get("t").approved_paths)

    _, applied, refused = apply_requests(registry, [
        {"kind": "context_ids", "id": "t",
         "context_ids": ["autoloop", "ctx-decision-01"]},
    ])

    assert (len(applied), refused) == (1, [])
    after = effective_approved_paths(registry.get("t").approved_paths)
    assert json.dumps(after) == json.dumps(before)
    assert unauthorized_paths({"autoloop/tasks.py"}, after) == {"autoloop/tasks.py"}


def test_a_context_ids_request_is_refused_while_the_dispatch_is_in_flight(tmp_path):
    """The same `_refuse_immutable` rule every other content mutation takes,
    with the control in the same batch: the queued task's correction still
    lands, so this is the guard refusing an in-flight edit rather than the kind
    being wired to nothing."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("context_ids", "running", ["ctx-decision-09"])
    inbox.submit_mutation("context_ids", "queued", ["ctx-decision-09"])

    registry = TaskRegistry()
    registry.add_many([
        Task(id="running", title="R", description="d",
             approved_paths=("autoloop/inbox.py",), context_ids=("ctx-decision-01",)),
        Task(id="queued", title="Q", description="d",
             approved_paths=("autoloop/inbox.py",)),
    ])
    registry.mark_in_progress("running")

    specs, _ = inbox.drain()
    _, applied, refused = apply_requests(registry, specs)

    assert len(refused) == 1 and "in progress" in refused[0], refused
    assert registry.get("running").context_ids == ("ctx-decision-01",)
    assert len(applied) == 1, applied
    assert registry.get("queued").context_ids == ("ctx-decision-09",)


def test_a_context_ids_request_carries_only_its_own_field(tmp_path):
    """The per-kind rule, on the new kind. `approved_paths` on a `context_ids`
    request is the one that matters: it would read as a scope edit smuggled in
    under a kind that moves none, and it is refused rather than dropped."""
    inbox = TaskInbox(tmp_path / "inbox")
    with pytest.raises(InboxError, match="carries only"):
        inbox.submit({"kind": "context_ids", "id": "t",
                      "context_ids": ["ctx-01"], "approved_paths": ["a.py"]})
    with pytest.raises(InboxError, match="needs 'context_ids' as a list"):
        inbox.submit({"kind": "context_ids", "id": "t", "context_ids": "ctx-01"})
    with pytest.raises(InboxError, match="needs 'context_ids'"):
        inbox.submit({"kind": "context_ids", "id": "t"})
    assert inbox.pending() == [], "nothing malformed should reach the queue"


def test_blocking_through_the_inbox_has_a_reverse_through_the_inbox(tmp_path):
    """A hold placed here writes no `blockers.Blocker` record, and
    `python -m autoloop answer` — the only route out of `blocked` — takes a
    blocker id. So without an `unblock` kind this vocabulary would write a
    state with no way back out of it."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])

    inbox.submit_mutation("block", "t", "waiting on the operator")
    _, applied, refused = apply_requests(registry, inbox.drain()[0])
    assert refused == [] and len(applied) == 1
    assert registry.state_of("t").value == "blocked_by_operator"

    inbox.submit_mutation("unblock", "t")
    _, applied, refused = apply_requests(registry, inbox.drain()[0])
    assert refused == [] and len(applied) == 1
    assert registry.state_of("t").value == "ready"


def test_the_inbox_reverse_will_not_release_a_loop_raised_quarantine(tmp_path):
    """The narrowing that keeps the reverse from being a bypass. A `task_fatal`
    quarantine is resolved by `answer`, which resolves the blocker record and
    unblocks the task together; releasing it from here would put the task back
    in the ready queue with its blocker still open."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])
    registry.block("t", "validation failed three times")

    inbox.submit_mutation("unblock", "t")
    _, applied, refused = apply_requests(registry, inbox.drain()[0])

    assert applied == []
    assert len(refused) == 1 and "autoloop answer" in refused[0]
    assert registry.get("t").blocked_reason == "validation failed three times"


def test_the_inbox_reverse_refuses_a_quarantine_that_merely_reads_like_a_hold(tmp_path):
    """The end-to-end twin of `test_tasks.py`'s registry regression, and the
    reason provenance is a stored field rather than the reason text.
    `blocked_reason` is free text the LOOP writes as well, so a park detail
    beginning with `OPERATOR_HOLD_PREFIX` used to make a real quarantine
    releasable from here — the blocker record left open and unanswered while
    the task went straight back into `ready_tasks()`."""
    from autoloop.inbox import apply_requests
    from autoloop.tasks import OPERATOR_HOLD_PREFIX

    inbox = TaskInbox(tmp_path / "inbox")
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])
    reason = OPERATOR_HOLD_PREFIX + "quoted from the agent's own report"
    registry.block("t", reason)

    inbox.submit_mutation("unblock", "t")
    _, applied, refused = apply_requests(registry, inbox.drain()[0])

    assert applied == []
    assert len(refused) == 1 and "autoloop answer" in refused[0]
    assert registry.get("t").blocked_reason == reason
    assert registry.state_of("t").value == "blocked_by_operator"


def test_a_creation_request_cannot_carry_a_mutation_field(tmp_path):
    """The other half of "a request carries only its own kind's fields".
    `reason` belongs to `block`, and a `task` request naming it meant a hold —
    checked against one GLOBAL field set it submitted cleanly and was then
    silently ignored on merge, which is precisely the outcome the per-kind rule
    exists to prevent. This is the submit gate; the merge gate runs the same
    check — see
    `test_a_hand_written_creation_carrying_a_mutation_field_is_refused_on_merge`."""
    inbox = TaskInbox(tmp_path / "inbox")
    with pytest.raises(InboxError, match="mutation-only"):
        inbox.submit({"kind": "task", "id": "t", "title": "T", "description": "D",
                      "reason": "hold this instead"})
    # Same for the no-kind legacy form, which is a creation request too.
    with pytest.raises(InboxError, match="unknown field"):
        inbox.submit({"id": "t", "title": "T", "description": "D", "reason": "x"})
    assert inbox.pending() == [], "nothing malformed should reach the queue"
    # The control: the same field on the kind that owns it is accepted.
    inbox.submit({"kind": "block", "id": "t", "reason": "hold this"})
    assert len(inbox.pending()) == 1


def test_one_shape_implementation_serves_both_gates():
    """The drift guard on the split, in the same style as the shared-merge and
    three-bucket guards above. `submit` is not the only gate: hand-writing the
    JSON file is the documented — and today the ONLY — operator route to five of
    the six mutation kinds, and such a file reaches `apply_requests` without
    ever passing through `submit`. Two shape implementations would mean the
    field an operator typed is refused by one route and silently ignored by the
    other, which is the whole defect the per-kind rule exists to prevent."""
    import ast
    import inspect
    import textwrap

    from autoloop import inbox

    for gate in (inbox.TaskInbox.submit, inbox.apply_requests):
        # An actual CALL node, not a substring of the source. Both of these
        # discuss the shared check at length in their docstrings, and a guard a
        # comment can satisfy guards nothing.
        tree = ast.parse(textwrap.dedent(inspect.getsource(gate)))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "check_request_shape" in called, (
            f"{gate.__qualname__} does not go through the shared shape check"
        )


def test_a_hand_written_creation_carrying_a_mutation_field_is_refused_on_merge():
    """`apply_requests` called DIRECTLY, which is what a hand-written file gets:
    `drain` hands it the parsed object and nothing else ran `submit`'s checks.
    The per-kind contract has to hold here too, or the route the vocabulary
    documents as the only one for the new kinds is the one route with no gate —
    this request used to be applied as a plain creation with the `reason` the
    author meant as a hold silently dropped."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, applied, refused = apply_requests(registry, [
        {"kind": "task", "id": "held", "title": "T", "description": "D",
         "reason": "hold this instead"},
        {"id": "queued-behind", "title": "Q", "description": "d"},
    ])

    assert len(refused) == 1, refused
    assert refused[0].startswith("held: "), refused[0]
    assert "mutation-only" in refused[0], refused[0]
    assert not registry.has("held"), "atomic: the task must not be half-created"
    assert applied == []
    assert [a.split(" ")[0] for a in added] == ["queued-behind"], (
        "the valid request queued behind it still lands"
    )


def test_a_hand_written_mutation_carrying_a_foreign_field_is_refused_atomically():
    """The mutation half of the same hole, and the one that shows why "atomic"
    needs asserting on BOTH fields: `apply_requests` used to read the key its
    kind names and ignore the rest, so this landed the hold and dropped the
    scope rewrite — a request that half-did what it said."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d",
                            approved_paths=("autoloop/inbox.py",))])

    added, applied, refused = apply_requests(registry, [
        {"kind": "block", "id": "t", "reason": "hold this",
         "approved_paths": ["autoloop/tasks.py"]},
        {"kind": "priority", "id": "t", "priority": 3},
    ])

    assert len(refused) == 1, refused
    assert "carries only" in refused[0], refused[0]
    task = registry.get("t")
    assert (task.status, task.blocked_reason, task.hold_origin) == ("pending", "", "")
    assert task.approved_paths == ("autoloop/inbox.py",), "neither field landed"
    assert added == []
    assert applied == ["t -> 3"], "the valid request queued behind it still lands"


def test_no_payload_field_falls_outside_both_per_kind_sets():
    """A drift guard on the split, not a behaviour test. Every mutation payload
    has to be either a creation field or declared mutation-only, or
    `ALLOWED_FIELDS` — the union `dashboard.TASK_REQUEST_FIELDS` documents
    itself against — quietly stops naming the whole vocabulary. Nothing
    validates against the union, which is exactly why nothing else would fail
    when a new kind forgets it."""
    from autoloop.inbox import (
        ALLOWED_FIELDS,
        CREATION_FIELDS,
        MUTATION_ONLY_FIELDS,
        MUTATION_PAYLOAD,
    )

    payloads = {p for p in MUTATION_PAYLOAD.values() if p is not None}
    assert payloads <= ALLOWED_FIELDS
    assert ALLOWED_FIELDS == CREATION_FIELDS | MUTATION_ONLY_FIELDS
    assert "reason" not in CREATION_FIELDS, "the leak this split closed"


def test_retire_is_not_in_the_vocabulary(tmp_path):
    """Deliberate and load-bearing. `retire` is written-once with no reverse by
    design, so an inbox request that reached it would be exactly the
    unblockable one-way state `block`/`unblock` are shaped to avoid."""
    from autoloop.inbox import KINDS

    assert "retire" not in KINDS
    with pytest.raises(InboxError, match="unknown kind"):
        TaskInbox(tmp_path / "inbox").submit({"kind": "retire", "id": "t"})


def test_requests_apply_in_submission_order_in_one_pass():
    """`drain` returns oldest-first and `apply_requests` makes a SINGLE pass in
    that order. Two consequences, both asserted here: the last write to a field
    wins, and a mutation queued before its target exists is REFUSED rather than
    held back — deferring would make the outcome depend on what else happened
    to be in the batch."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, applied, refused = apply_requests(registry, [
        {"kind": "priority", "id": "late", "priority": 1},          # before it exists
        {"id": "late", "title": "L", "description": "d"},
        {"kind": "priority", "id": "late", "priority": 5},
        {"kind": "priority", "id": "late", "priority": 2},          # last one wins
    ])

    assert len(added) == 1
    assert len(refused) == 1 and "no task with id 'late'" in refused[0]
    assert applied == ["late -> 5", "late -> 2"]
    assert registry.get("late").priority == 2


def test_a_hand_written_request_with_an_unknown_kind_is_named_not_guessed_at():
    """`submit` refuses an unknown kind, so this is only reachable from a file
    an operator wrote by hand. Falling through to the creation branch would
    refuse it for whichever unrelated field it happens to lack, sending the
    reader after the wrong problem."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])
    _, applied, refused = apply_requests(registry, [
        {"kind": "retire", "id": "t"},
        # An UNHASHABLE kind. `kind in MUTATION_PAYLOAD` is a dict lookup, so
        # this raises `TypeError: unhashable type` unless the string check runs
        # first — and it would raise from outside the per-request try, taking
        # the whole drain (and the running loop's step) down with one file.
        {"kind": [], "id": "t"},
        {"id": "later", "title": "L", "description": "d"},
    ])

    assert applied == []
    assert len(refused) == 2, refused
    assert "unknown kind 'retire'" in refused[0]
    assert "unknown kind []" in refused[1]
    assert registry.has("later"), "the request queued behind them still landed"


def test_the_middle_bucket_stays_one_bucket_both_callers_already_save_on():
    """Not cosmetic. Both drain call sites unpack three values positionally and
    gate `task_store.save()` on `if added or <middle>`; a fourth bucket either
    of them forgot to add to that condition would apply a mutation in memory
    and never persist it, and the next in-memory save would overwrite it
    silently. One bucket cannot be half-wired."""
    import inspect

    from autoloop import cli, orchestrator
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])
    assert len(apply_requests(registry, [])) == 3, "three buckets, not four"

    for source in (inspect.getsource(orchestrator.Orchestrator._drain_task_inbox),
                   inspect.getsource(cli._cmd_drain_inbox)):
        call = next(ln for ln in source.splitlines() if "apply_requests(" in ln)
        names = [n.strip() for n in call.split("=")[0].split(",")]
        assert len(names) == 3, f"caller unpacks {names}, not three buckets"
        assert f"if {names[0]} or {names[1]}:" in source, (
            f"this caller does not persist its {names[1]!r} bucket"
        )


def test_the_cli_actually_builds_an_orchestrator(tmp_path, monkeypatch):
    """The gap that let a broken `run` ship: every other test constructs the
    Orchestrator directly, so nothing exercised `_build_orchestrator` /
    `_build_executor`. A keyword landing on the wrong constructor (task_inbox
    was passed to ImplementExecutor) type-errors only at real startup.

    Builds the real collaborator set against a throwaway repo — no browser, no
    agent, no network: construction is the whole assertion.
    """
    import subprocess

    from autoloop import cli
    from autoloop.config import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "work"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)

    # `_build_orchestrator` provisions the publisher repo, which needs a real
    # remote to snapshot a url from.
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(upstream)], cwd=repo,
                   check=True, capture_output=True)

    (repo / ".autoloop").mkdir()
    (repo / ".autoloop" / "config.toml").write_text(
        '[browser]\nconversation_url = "https://chatgpt.com/c/abc"\n\n'
        f'[paths]\nworkers_root = "{tmp_path / "outside" / "workers"}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    config = load_config(repo / ".autoloop" / "config.toml")
    store, state = cli._load_state(config)
    if state is None:
        from autoloop.state import LoopState, StateStore

        state = LoopState.new(config.browser.conversation_url)
        store = StateStore(config.state_file)
        store.save(state)
    task_store, registry = cli._load_tasks(config)

    orch = cli._build_orchestrator(
        config, argparse_ns(config), store, state, task_store, registry
    )
    assert orch._task_inbox is not None, "the inbox must reach the Orchestrator"
    assert orch._task_inbox.directory.is_absolute()


def argparse_ns(config):
    import argparse

    return argparse.Namespace(config=config.state_dir / "config.toml", null_executor=True)


def test_add_task_takes_context_id_repeatably_like_its_neighbours():
    """Parser wiring, asserted against the SAME shape `--approved-path` and
    `--depends-on` have: `action="append"`, defaulting to an empty list. A flag
    that overwrote instead of appending would silently keep only the last
    record an operator named."""
    from autoloop import cli

    args = cli.build_parser().parse_args([
        "add-task", "--id", "t", "--title", "T", "--description", "d",
        "--context-id", "ctx-decision-01", "--context-id", "ctx-incident-02",
        "--approved-path", "autoloop/tasks.py",
    ])
    assert args.context_id == ["ctx-decision-01", "ctx-incident-02"]
    assert cli.build_parser().parse_args(
        ["add-task", "--id", "t", "--title", "T", "--description", "d"]
    ).context_id == []


def test_a_seed_tasks_row_can_name_its_context_and_a_bare_string_is_refused(tmp_path):
    """The OTHER `Task` construction site, and it has the defect its own
    comment warns about for `validation`/`validation_cwd`: a field the seed file
    declares and `_seed_registry` forgets is dropped in silence. Driven through
    the real function, both ways — the ids land, and a bare string is refused by
    the registry rather than becoming one id per character."""
    import types

    from autoloop import cli

    seed = tmp_path / "seed_tasks.json"
    seed.write_text(json.dumps([
        {"id": "s1", "title": "T", "description": "d",
         "approved_paths": ["autoloop/tasks.py"],
         "context_ids": ["ctx-decision-01"]},
    ]), encoding="utf-8")
    registry = cli._seed_registry(types.SimpleNamespace(seed_tasks_file=seed))
    assert registry.get("s1").context_ids == ("ctx-decision-01",)
    assert registry.get("s1").approved_paths == ("autoloop/tasks.py",)

    seed.write_text(json.dumps([
        {"id": "s2", "title": "T", "description": "d", "context_ids": "ctx01"},
    ]), encoding="utf-8")
    from autoloop.errors import TaskGraphError

    with pytest.raises(TaskGraphError, match="context_ids"):
        cli._seed_registry(types.SimpleNamespace(seed_tasks_file=seed))


@pytest.mark.parametrize("bad", FALSY_NOT_A_LIST, ids=FALSY_IDS)
def test_a_falsy_seed_context_ids_is_refused_rather_than_emptied(tmp_path, bad):
    """The same fail-open on the OTHER construction site, and the one where it
    is least likely to be noticed: a seed file is read once, at a brand-new
    deployment, so a reference list quietly emptied there is provenance nobody
    ever sees go missing. It fails loudly instead, exactly as the bare string
    above does."""
    import types

    from autoloop import cli
    from autoloop.errors import TaskGraphError

    seed = tmp_path / "seed_tasks.json"
    seed.write_text(json.dumps([
        {"id": "s1", "title": "T", "description": "d", "context_ids": bad},
    ]), encoding="utf-8")

    with pytest.raises(TaskGraphError, match="context_ids"):
        cli._seed_registry(types.SimpleNamespace(seed_tasks_file=seed))


def test_a_seed_row_may_cite_nothing_in_any_of_its_three_spellings(tmp_path):
    """The seed half of "not everything falsy is malformed". A row carrying
    `[]`, a row with no `context_ids` at all (which is every `seed_tasks.json`
    written before ctx-04) and a hand-edited `null` all load as "cites no
    record" rather than being refused."""
    import types

    from autoloop import cli

    seed = tmp_path / "seed_tasks.json"
    seed.write_text(json.dumps([
        {"id": "empty", "title": "T", "description": "d", "context_ids": []},
        {"id": "absent", "title": "T", "description": "d"},
        {"id": "nulled", "title": "T", "description": "d", "context_ids": None},
    ]), encoding="utf-8")

    registry = cli._seed_registry(types.SimpleNamespace(seed_tasks_file=seed))

    for task_id in ("empty", "absent", "nulled"):
        assert registry.get(task_id).context_ids == (), task_id
        assert isinstance(registry.get(task_id).context_ids, tuple), task_id


def test_add_task_context_id_round_trips_into_the_registry(tmp_path, monkeypatch):
    """The whole operator route, end to end: the real CLI writes a real request
    into a real inbox, and the real merge puts the ids on the real task. And
    the scope it lands with is exactly the one `--approved-path` named — the
    two flags are adjacent on the command line and must not be adjacent in
    effect."""
    from gitrepo import make_repo_from_template

    from autoloop import cli
    from autoloop.inbox import apply_requests

    repo = make_repo_from_template(tmp_path / "repo")
    workers_root = tmp_path / "outside" / "workers"
    config = tmp_path / "config.toml"
    config.write_text(
        "[conversation]\n"
        'provider = "codex_cli"\n'
        "[paths]\n"
        f'state_dir = "{tmp_path / "state"}"\n'
        f'workers_root = "{workers_root}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    assert cli.main([
        "add-task", "--config", str(config), "--id", "ctx-demo",
        "--title", "T", "--description", "d",
        "--approved-path", "autoloop/tasks.py",
        "--context-id", "ctx-decision-01", "--context-id", "ctx-incident-02",
    ]) == 0

    specs, problems = TaskInbox(workers_root.parent / "inbox").drain()
    registry = TaskRegistry()
    added, _applied, refused = apply_requests(registry, specs)

    assert (problems, refused, len(added)) == ([], [], 1)
    task = registry.get("ctx-demo")
    assert task.context_ids == ("ctx-decision-01", "ctx-incident-02")
    assert task.approved_paths == ("autoloop/tasks.py",)


# ---- claims, precedence and conflicts (ctx-06) ------------------------------
#
# The primitives `audit/taskgen.py` enforces the planning discipline with. Tested
# here, as pure functions over values, because that is what they are: no
# repository, no subprocess and no agent round can make any of these claims fail
# for a different reason than the one being asserted.


from autoloop.inbox import (  # noqa: E402 — grouped with the section it serves
    CLAIM_BEHAVIOUR,
    CLAIM_CONSTRAINT,
    CLAIM_INTENT,
    SCOPE_CHANGED,
    SCOPE_IMPACT_UNKNOWN,
    SCOPE_UNCHANGED,
    SOURCE_ACCEPTED_DECISION,
    SOURCE_CONTEXT_RECORD,
    SOURCE_MODEL,
    SOURCE_OPERATOR_REQUEST,
    SOURCE_PRECEDENCE,
    SOURCE_REPOSITORY,
    Claim,
    Evidence,
    SourceConflict,
    detect_conflicts,
    evidence_reader,
    has_location,
    source_rank,
    unsupported_claims,
)


def repo_claim(text="the gate returns True", **overrides):
    base = dict(
        source=SOURCE_REPOSITORY,
        subject="s",
        kind=CLAIM_BEHAVIOUR,
        citation=Evidence(text="autoloop/policy.py:12", source="git show"),
    )
    base.update(overrides)
    return Claim(text=text, **base)


def test_a_model_is_never_a_source_of_evidence():
    """`Evidence`'s own docstring rule, reaching the generator. Not a lower tier
    of evidence — not evidence, and not in the precedence order at all."""
    assert SOURCE_MODEL not in SOURCE_PRECEDENCE
    for label in ("model", "the assistant", "chat history", "model memory", "Claude"):
        assert evidence_reader(label) == "", label


def test_an_unreadable_provenance_never_outranks_one_somebody_classified():
    """Fail-closed ranking: unknown, empty and non-string all rank LAST."""
    assert source_rank(SOURCE_OPERATOR_REQUEST) == 0
    assert source_rank(SOURCE_REPOSITORY) < source_rank(SOURCE_CONTEXT_RECORD)
    for unclassified in (SOURCE_MODEL, "", None, 7, ["repository"]):
        assert source_rank(unclassified) == len(SOURCE_PRECEDENCE), unclassified


def test_a_path_is_a_location_not_a_reader():
    """The hole a shape test would leave: `source` names the READER, so an
    invented `autoloop/inbox.py:1545` must not walk in by being path-shaped."""
    assert evidence_reader("autoloop/inbox.py:1545") == ""
    assert evidence_reader("git ls-files") == "git ls-files"
    assert evidence_reader("  GIT LS-FILES  ") == "git ls-files", "normalised"
    assert evidence_reader("something plausible") == ""
    assert evidence_reader(None) == ""


def test_prose_is_not_a_citation():
    """`has_location` decides whether a citation names somewhere to look. It
    must not be satisfiable by ordinary English — `e.g.` was the case that made
    the extension bound `{2,6}` rather than `{1,6}`."""
    for cited in ("a.py:12", "docs/AUTOLOOP.md", "autoloop/inbox.py:1545",
                  "see autoloop/tests/test_x.py:10-40.", "it is in a.py."):
        assert has_location(cited), cited
    for prose in ("saw it", "e.g. it breaks", "i.e. the loop stops",
                  "It fails. The next round repeats it.", "", None):
        assert not has_location(prose), prose


def test_an_uncited_repository_claim_is_refused_and_the_refusal_names_it():
    [refusal] = unsupported_claims([repo_claim("the gate returns True", citation=None)])
    assert "the gate returns True" in refusal
    assert "no citation" in refusal


def test_the_same_claim_with_an_assumption_stands():
    assert unsupported_claims([
        repo_claim(citation=None, assumption="not verified; assuming it still holds")
    ]) == ()


def test_readiness_is_never_inferred_from_an_empty_list():
    """Positive-only, like `draft_blockers`: no claims means nothing to refuse,
    which is not the same as anything having been supported."""
    assert unsupported_claims([]) == ()
    assert unsupported_claims([repo_claim(text="   ")])[0].startswith("a claim with no text")


def test_the_guard_cannot_be_switched_off_by_declaring_a_claim_non_repository():
    """`repository_specific=False` is the flag an author reaches for to quiet a
    refusal, and text naming a file is exactly the text the refusal is for."""
    quiet = repo_claim("rewrite autoloop/policy.py", citation=None,
                       repository_specific=False)
    assert unsupported_claims([quiet]), "a claim naming a file is a repository claim"
    # The control: a sentence about nothing in this tree really is exempt.
    assert unsupported_claims([
        Claim(text="prefer the simpler wording", source=SOURCE_REPOSITORY,
              repository_specific=False)
    ]) == ()


def test_what_the_operator_asked_for_is_not_evidence_of_what_the_code_does():
    """Precedence line 3, enforced rather than described."""
    [refusal] = unsupported_claims([
        repo_claim(citation=Evidence(text="they asked for it",
                                     source="the operator's request"))
    ])
    assert "not evidence of current behaviour" in refusal
    # The same citation is fine for a claim about what is WANTED.
    assert unsupported_claims([
        repo_claim(kind=CLAIM_INTENT,
                   citation=Evidence(text="they asked for it",
                                     source="the operator's request"))
    ]) == ()


def test_a_context_record_is_not_believed_until_something_was_read():
    """Precedence line 4. A record citing only itself is navigation, not
    evidence; verified against the tree, or stated as an assumption, it stands."""
    unverified = repo_claim(
        source=SOURCE_CONTEXT_RECORD,
        citation=Evidence(text="ctx-42 says so", source="the context record index"),
    )
    [refusal] = unsupported_claims([unverified])
    assert "without being verified" in refusal

    verified = repo_claim(
        source=SOURCE_CONTEXT_RECORD,
        citation=Evidence(text="autoloop/policy.py:12", source="git show"),
    )
    assert unsupported_claims([verified]) == ()


def test_a_reader_whose_content_is_agent_authored_needs_a_location():
    """The audit report is a real file, and what is inside it is a model's
    sentence. The location is what a reviewer can check, so it is required."""
    [refusal] = unsupported_claims([
        repo_claim(citation=Evidence(text="I saw it", source="the audit report"))
    ])
    assert "no location" in refusal
    assert unsupported_claims([
        repo_claim(citation=Evidence(text="a.py:10 I saw it", source="the audit report"))
    ]) == ()


def scope_claim(source, paths, text, kind=CLAIM_BEHAVIOUR, author=""):
    return Claim(
        text=text, source=source, author=author, subject="the scope", kind=kind,
        citation=Evidence(text="a.py:1", source="git show"), paths=paths,
    )


def test_a_conflict_keeps_both_sides_and_picks_no_winner():
    left = scope_claim(SOURCE_REPOSITORY, ("a.py",), "the fix touches a.py")
    right = scope_claim(SOURCE_ACCEPTED_DECISION, ("b.py",), "it touches b.py",
                        kind=CLAIM_CONSTRAINT)

    [conflict] = detect_conflicts([left, right])

    assert {conflict.left.source, conflict.right.source} == {
        SOURCE_REPOSITORY, SOURCE_ACCEPTED_DECISION
    }
    assert conflict.scope_impact == SCOPE_CHANGED
    assert conflict.stops_generation is True
    described = conflict.describe()
    assert "a.py" in described and "b.py" in described
    assert "No winner was chosen" in described


def test_precedence_never_resolves_a_conflict():
    """The order exists to say what settles what, never to pick a side. The
    higher-ranked source does not make the disagreement go away."""
    operator = scope_claim(SOURCE_OPERATOR_REQUEST, ("a.py",), "only a.py",
                           kind=CLAIM_INTENT)
    tree = scope_claim(SOURCE_REPOSITORY, ("b.py",), "b.py too")

    assert len(detect_conflicts([operator, tree])) == 1
    assert len(detect_conflicts([tree, operator])) == 1, "and order does not matter"


def test_an_unmeasurable_scope_impact_stops_rather_than_passes():
    """The tri-state, and the reason it is not a boolean: a `changes_scope`
    defaulting to False would report every conflict it could not measure as
    harmless, satisfying the rule vacuously."""
    silent = scope_claim(SOURCE_OPERATOR_REQUEST, (), "the loop is too slow",
                         kind=CLAIM_INTENT)
    tree = scope_claim(SOURCE_REPOSITORY, ("a.py",), "a.py is the hot path")

    [conflict] = detect_conflicts([silent, tree])
    assert conflict.scope_impact == SCOPE_IMPACT_UNKNOWN
    assert conflict.stops_generation is True


def test_an_accepted_scope_that_covers_the_work_is_not_a_disagreement():
    """Coverage, not equality — and through `tasks.unauthorized_paths`, the same
    matcher the pre-commit gate uses, so the directory rule cannot drift.

    A constraint and an observed behaviour are two different questions, so the
    words are not compared at all: they differ for every finding ever written,
    and that difference IS the defect being reported."""
    wider = scope_claim(SOURCE_ACCEPTED_DECISION, ("autoloop/",),
                        "scoped to autoloop/", kind=CLAIM_CONSTRAINT)
    needed = scope_claim(SOURCE_REPOSITORY, ("autoloop/policy.py",), "policy.py changes")

    assert detect_conflicts([wider, needed]) == ()

    # The other direction IS a disagreement: a file the accepted scope cannot
    # reach is a task that cannot do what it was filed for.
    narrower = scope_claim(SOURCE_ACCEPTED_DECISION, ("autoloop/policy.py",),
                           "scoped to policy.py", kind=CLAIM_CONSTRAINT)
    two_files = scope_claim(SOURCE_REPOSITORY, ("autoloop/policy.py", "autoloop/cli.py"),
                            "both files change")
    [conflict] = detect_conflicts([narrower, two_files])
    assert conflict.scope_impact == SCOPE_CHANGED
    assert conflict.stops_generation is True


def test_coverage_is_measured_even_where_no_conflict_is_reported():
    """`scope_impact` is a property of the PAIR, and it is what the
    different-kind rule consults before deciding there is nothing to report.
    Asserted directly, because that pair is (rightly) not returned as a conflict
    — so a coverage rule that had quietly become "always CHANGED" would still
    leave `detect_conflicts` looking correct on the covered case."""
    covered = SourceConflict(
        subject="s",
        left=scope_claim(SOURCE_ACCEPTED_DECISION, ("autoloop/",), "scoped",
                         kind=CLAIM_CONSTRAINT),
        right=scope_claim(SOURCE_REPOSITORY, ("autoloop/policy.py",), "changes"),
    )

    assert covered.scope_impact == SCOPE_UNCHANGED
    assert covered.stops_generation is False


def test_a_finding_is_not_reported_as_a_source_conflict():
    """The noise that would switch this guard off. Every finding says the code
    does X while an accepted decision says X must not happen — if that counted,
    every finding would stop generation and the feature would be turned off
    within a day."""
    constraint = scope_claim(SOURCE_ACCEPTED_DECISION, ("a.py",),
                             "the gate must stay closed", kind=CLAIM_CONSTRAINT)
    observed = scope_claim(SOURCE_REPOSITORY, ("a.py",), "the gate is open")

    assert detect_conflicts([constraint, observed]) == ()


def test_one_source_saying_several_things_is_not_a_disagreement_with_itself():
    """The limit stated in `detect_conflicts`: ONE AUTHOR saying two things is
    elaborating, not contradicting. Without this every finding — which says what
    it saw AND what it wants AND what it assumed — reports itself as
    self-contradictory and all generation stops."""
    assert detect_conflicts([
        scope_claim(SOURCE_REPOSITORY, ("a.py",), "the gate returns True"),
        scope_claim(SOURCE_REPOSITORY, ("a.py",), "the fix adds a check"),
    ]) == ()
    # And with the author said out loud, which is what `Finding.claims` does.
    assert detect_conflicts([
        scope_claim(SOURCE_REPOSITORY, ("a.py",), "the gate returns True", author="d1:f1"),
        scope_claim(SOURCE_REPOSITORY, ("a.py",), "the fix adds a check", author="d1:f1"),
    ]) == ()


def test_two_authors_in_one_tier_can_disagree():
    """The hole the different-SOURCE rule left: two ACCEPTED TASKS scoping one
    piece of work to two different file sets share a tier, so every pair of them
    was suppressed — hiding exactly the disagreement an operator has to settle.
    The unit is the author, not the tier."""
    one = scope_claim(SOURCE_ACCEPTED_DECISION, ("a.py",), "scoped to a.py",
                      kind=CLAIM_CONSTRAINT, author="task-one")
    two = scope_claim(SOURCE_ACCEPTED_DECISION, ("b.py",), "scoped to b.py",
                      kind=CLAIM_CONSTRAINT, author="task-two")

    [conflict] = detect_conflicts([one, two])

    assert conflict.scope_impact == SCOPE_CHANGED
    assert conflict.stops_generation is True
    # BOTH tasks named: the tier is identical on both sides, so a record that
    # printed only the source would name neither of the two to go and reconcile.
    described = conflict.describe()
    assert "task-one" in described and "task-two" in described


def test_two_authors_in_one_tier_agreeing_is_not_a_conflict():
    """The control for the test above, and the noise that would switch the guard
    off: two accepted tasks that scope one job the same way agree, however many
    of them there are."""
    assert detect_conflicts([
        scope_claim(SOURCE_ACCEPTED_DECISION, ("a.py",), "scoped to a.py",
                    kind=CLAIM_CONSTRAINT, author="task-one"),
        scope_claim(SOURCE_ACCEPTED_DECISION, ("a.py",), "scoped to a.py",
                    kind=CLAIM_CONSTRAINT, author="task-two"),
    ]) == ()


def test_two_within_tier_conflicts_do_not_collide_on_one_identity():
    """The digest keys the durable record, and within one tier both sides carry
    the same source — so without the author two disagreements whose texts happen
    to coincide would file as ONE record, and the second would overwrite the
    first one's account of who disagreed."""
    conflicts = detect_conflicts([
        scope_claim(SOURCE_ACCEPTED_DECISION, ("a.py",), "scoped narrowly",
                    kind=CLAIM_CONSTRAINT, author="task-one"),
        scope_claim(SOURCE_ACCEPTED_DECISION, ("b.py",), "scoped narrowly",
                    kind=CLAIM_CONSTRAINT, author="task-two"),
        scope_claim(SOURCE_ACCEPTED_DECISION, ("c.py",), "scoped narrowly",
                    kind=CLAIM_CONSTRAINT, author="task-three"),
    ])

    assert len(conflicts) == 3
    assert len({c.identity for c in conflicts}) == 3


def test_one_sentence_can_still_be_two_scopes():
    """Agreeing is not the same as saying the same words. Skipping on the text
    alone dropped the MATERIAL case — one sentence, two file lists — because the
    two authors happened to word it alike."""
    same_words = "fix the unreadable-file gate"
    [conflict] = detect_conflicts([
        scope_claim(SOURCE_REPOSITORY, ("autoloop/policy.py",), same_words),
        scope_claim(SOURCE_ACCEPTED_DECISION, ("autoloop/inbox.py",), same_words,
                    kind=CLAIM_CONSTRAINT),
    ])
    assert conflict.scope_impact == SCOPE_CHANGED

    # And the control, or this would report every agreement as a conflict: two
    # sources answering ONE question in the same words, with nothing positively
    # different about their scope, agree.
    assert detect_conflicts([
        scope_claim(SOURCE_REPOSITORY, (), same_words),
        scope_claim(SOURCE_ACCEPTED_DECISION, (), same_words),
    ]) == ()


def test_claims_about_different_subjects_are_never_compared():
    assert detect_conflicts([
        Claim(text="alpha", source=SOURCE_REPOSITORY, subject="one"),
        Claim(text="beta", source=SOURCE_ACCEPTED_DECISION, subject="two"),
    ]) == ()


def test_two_spellings_of_one_conflict_share_an_identity():
    """The digest the durable record is keyed on: a re-wrap must not read as a
    second, different disagreement, and two genuinely different ones must not
    collapse into one record."""
    a = detect_conflicts([
        scope_claim(SOURCE_REPOSITORY, ("a.py",), "The fix touches a.py!"),
        scope_claim(SOURCE_ACCEPTED_DECISION, ("b.py",), "it touches b.py",
                    kind=CLAIM_CONSTRAINT),
    ])[0]
    b = detect_conflicts([
        scope_claim(SOURCE_REPOSITORY, ("a.py",), "the fix touches   a.py"),
        scope_claim(SOURCE_ACCEPTED_DECISION, ("b.py",), "it touches b.py",
                    kind=CLAIM_CONSTRAINT),
    ])[0]
    c = detect_conflicts([
        scope_claim(SOURCE_REPOSITORY, ("a.py",), "something else entirely"),
        scope_claim(SOURCE_ACCEPTED_DECISION, ("b.py",), "it touches b.py",
                    kind=CLAIM_CONSTRAINT),
    ])[0]

    assert a.identity == b.identity
    assert a.identity != c.identity
