"""`[concurrency]` — the fleet size, and the lane vocabulary it names.

Candidate 1 of the nine in docs/AUTOLOOP.md, "Running several tasks at once —
the split plan". The claim is deliberately small: **a `lanes` setting exists, is
validated, and changes nothing at `1`.** Nothing reads it yet, so this file is
where the setting is held to its whole contract, in three parts:

1. **At `1` it is not there.** An absent section and an explicit `lanes = 1`
   produce EQUAL `AutoloopConfig` objects, so no deployment can acquire
   different behaviour by upgrading past this commit. The rest of the suite
   covers the same claim from the other side — every existing test runs against
   a config that never mentions the section.
2. **A fleet size the loop would not run is REFUSED, and the refusal names the
   key.** `cli.main` prints `error: <exc>` and nothing else, so "which key did I
   get wrong" has to be answerable from the message alone. Nothing is clamped:
   a clamped value reads as configured while a different fleet runs.
3. **A lane id can never be a task id.** Lane ids become path components beside
   directories addressed by task id (`workers_root/<task_id>`), and the two
   namespaces are kept apart BY CONSTRUCTION — a lane id starts with `_`, which
   `validate_task_id` refuses outright. The tests below go through the real
   consumers rather than asserting on the prefix, so the property survives
   someone changing the prefix and holds only while it is genuinely disjoint.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from autoloop.config import (
    LANE_ID_PREFIX,
    MAX_LANES,
    AutoloopConfig,
    ConcurrencyConfig,
    lane_id,
    load_config,
)
from autoloop.errors import ConfigError
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktree import WorktreeManager, validate_task_id

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.toml"


def write_config(tmp_path: Path, body: str = "") -> Path:
    """A minimal loadable config, plus whatever body is under test.

    Same shape as `test_config_repo_section.write_config`: `workers_root` is the
    one required key, and nothing here touches a real repository — this section
    is validated entirely at load time, so a `tmp_path` string is the whole
    fixture the claim needs.
    """
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nworkers_root = "{tmp_path / "w"}"\n\n' + body, encoding="utf-8"
    )
    return path


# ---- 1. at `lanes = 1` nothing is different ---------------------------------


def test_an_absent_section_is_one_lane(tmp_path):
    """Every config file written before this section existed, which is all of
    them: the template is copied once and never re-read."""
    assert load_config(write_config(tmp_path)).concurrency.lanes == 1


def test_an_empty_section_is_one_lane(tmp_path):
    """A header with no keys under it — what an operator leaves behind after
    deleting a value — is the default, not a fleet of nothing."""
    assert load_config(write_config(tmp_path, "[concurrency]\n")).concurrency.lanes == 1


def test_the_dataclass_default_is_one():
    """The default that matters for the ~fifty direct `AutoloopConfig(...)`
    constructions in this suite and for `doctor`: none of them names the field,
    and all of them must keep meaning the single-lane loop."""
    assert ConcurrencyConfig().lanes == 1
    assert AutoloopConfig.__dataclass_fields__["concurrency"].default.lanes == 1


def test_the_field_was_appended_so_positional_construction_is_unmoved():
    """`observed_checkout`'s own comment states the rule this pins: the
    positional meaning of every earlier field must not move, because the direct
    `AutoloopConfig(...)` sites across the suite are not all keyword-only. Stated
    as "after everything that predates it" rather than "last", so a later
    candidate may append its own field without editing this."""
    names = [f.name for f in dataclasses.fields(AutoloopConfig)]
    assert names.index("concurrency") > names.index("notify")


def test_absent_and_an_explicit_one_load_to_equal_configs(tmp_path):
    """THE compatibility claim, stated as an equality rather than as a survey of
    fields — a field added later that behaved differently at `1` would fail this
    without anyone remembering to extend it.

    Both loads use the SAME config path, so `legacy_state_dir_for` and every
    derived path resolve identically and the only difference between the two
    documents is the section under test.
    """
    absent = load_config(write_config(tmp_path))
    explicit = load_config(write_config(tmp_path, "[concurrency]\nlanes = 1\n"))

    assert absent == explicit


# ---- 2. what the loader refuses, and how it says so -------------------------


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-1",
        '"two"',
        "1.5",
        # A float that happens to be whole. TOML types it as a float, and
        # accepting it would make `2.0` and `2` two spellings of one setting
        # while `1.5` was refused — a distinction nobody could predict.
        "2.0",
        # `true` IS 1 in Python. Refused rather than read as a fleet of one:
        # this is a count, and a boolean here means someone thought it a switch.
        "true",
        # One past the ceiling, spelled from the constant so this case moves
        # with it rather than agreeing with it today and disagreeing later.
        str(MAX_LANES + 1),
        # The digit-slip the ceiling exists for.
        "40",
    ],
)
def test_an_unusable_fleet_size_is_refused_and_the_message_names_the_key(
    tmp_path, value
):
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, f"[concurrency]\nlanes = {value}\n"))
    assert "concurrency.lanes" in str(exc.value), (
        "the refusal must name the key: `cli.main` prints `error: <exc>` and "
        "nothing else, so this message is all the operator gets"
    )


def test_the_ceiling_is_inclusive(tmp_path):
    """The ceiling is the largest ACCEPTED value, not the first refused one.
    Pinned from both sides because an off-by-one here is invisible until an
    operator picks exactly the boundary."""
    at_ceiling = load_config(
        write_config(tmp_path, f"[concurrency]\nlanes = {MAX_LANES}\n")
    )
    assert at_ceiling.concurrency.lanes == MAX_LANES

    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, f"[concurrency]\nlanes = {MAX_LANES + 1}\n"))
    assert str(MAX_LANES) in str(exc.value), "the message should say what the limit is"


def test_every_value_in_range_loads(tmp_path):
    """Nothing between 1 and the ceiling is refused. The setting is inert today,
    so this is the cheapest guard against a validator that accidentally admits
    only the default."""
    for lanes in range(1, MAX_LANES + 1):
        config = load_config(write_config(tmp_path, f"[concurrency]\nlanes = {lanes}\n"))
        assert config.concurrency.lanes == lanes


def test_a_bare_key_is_reported_as_a_malformed_section(tmp_path):
    """`concurrency = "two"` written without a header. Without the shape check
    this reaches `_check_keys`, which would report the LETTERS of the string as
    unknown keys — true, and useless.

    Written by hand rather than through `write_config`: a bare key has to come
    BEFORE the first table header, or TOML reads it as a key inside `[paths]`
    and this tests something else entirely.
    """
    path = tmp_path / "config.toml"
    path.write_text(
        f'concurrency = "two"\n\n[paths]\nworkers_root = "{tmp_path / "w"}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    message = str(exc.value)
    assert "[concurrency] must be a table" in message
    assert "unknown keys" not in message


def test_an_unknown_key_in_the_section_is_refused(tmp_path):
    """Strict by design, exactly as every other section is: a typo'd fleet size
    must never be a key that loads and does nothing."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, "[concurrency]\nlane = 2\n"))
    assert "unknown keys in [concurrency]" in str(exc.value)


def test_the_section_is_still_strict_about_being_unknown(tmp_path):
    """The other half of adding a section name to `_SECTIONS`: neighbouring
    misspellings stay refused rather than joining it."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, "[concurrent]\nlanes = 2\n"))
    assert "unknown config sections" in str(exc.value)


# ---- 3. the lane vocabulary --------------------------------------------------


def test_a_lane_id_is_derived_deterministically():
    """Every per-lane path the plan describes hangs off this string, so lane 3
    has to be `_lane-3` in the next process as well as in this one."""
    assert lane_id(0) == f"{LANE_ID_PREFIX}0"
    assert lane_id(3) == lane_id(3) == f"{LANE_ID_PREFIX}3"
    ids = [lane_id(i) for i in range(MAX_LANES)]
    assert len(set(ids)) == len(ids), "two lanes would share a state file and a clone"


@pytest.mark.parametrize("index", [-1, True, False, 1.0, "0", None])
def test_a_lane_index_that_is_not_a_lane_is_refused(index):
    """`lane_id(True)` would be a second spelling of lane 1 and `lane_id(-1)` a
    directory named after a lane that does not exist — both formatted happily by
    an f-string, which is why the refusal is explicit."""
    with pytest.raises(ValueError):
        lane_id(index)


@pytest.mark.parametrize("index", range(MAX_LANES + 1))
def test_a_lane_id_is_refused_as_a_task_id(index):
    """The namespace claim, at every lane the ceiling admits. Parametrized over
    the range rather than asserted for one digit so the property is pinned to
    the PREFIX, which is what makes it hold, and not to `_lane-0`."""
    with pytest.raises(ValueError):
        validate_task_id(lane_id(index))


def test_a_lane_id_can_never_name_a_worker_repo_or_a_worktree(tmp_path):
    """Through the real consumers, which is the claim that matters: the
    directories under `workers_root` are addressed by task id, so if a lane id
    were a valid one, a lane's own tree and a task's worker repository could be
    the same directory. `default_observed_checkout` records this exact shape of
    bug for a fixed name — one `add-task --id observed-checkout` away."""
    workers = WorkerRepoManager(tmp_path / "workers", tmp_path / "hooks")
    worktrees = WorktreeManager(git=None, root_dir=tmp_path / "trees")

    for index in range(MAX_LANES):
        with pytest.raises(ValueError):
            workers.path_for(lane_id(index))
        with pytest.raises(ValueError):
            workers.hooks_dir_for(lane_id(index))
        with pytest.raises(ValueError):
            worktrees.path_for(lane_id(index))
        with pytest.raises(ValueError):
            worktrees.branch_for(lane_id(index))


def test_a_lane_id_is_a_single_safe_path_component():
    """It is appended to a path (`lanes/<lane_id>/state.json`, the per-lane
    observed checkout), so it must be ONE directory name: no separator, no
    traversal, no leading dash for an argv parser to read as a flag."""
    for index in range(MAX_LANES):
        name = lane_id(index)
        assert "/" not in name and "\\" not in name
        assert ".." not in name
        assert not name.startswith("-")
        assert name.strip() == name and name


# ---- the template ------------------------------------------------------------


def test_the_template_ships_one_lane_and_documents_the_key():
    """The template is copied once and never re-read, so one that shipped a
    larger fleet would hand every new deployment a setting the loop cannot yet
    honour — and candidate 9, not this one, is where concurrency is turned on."""
    example = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    section = example.split("[concurrency]", 1)[1].split("\n[", 1)[0]

    assert "lanes = 1" in section
    for field in dataclasses.fields(ConcurrencyConfig):
        assert f"{field.name} =" in section, f"the template does not document {field.name}"
    # The ceiling, in the file an operator actually reads. Spelled from the
    # constant so a raised limit cannot leave the template quoting the old one.
    assert str(MAX_LANES) in section
