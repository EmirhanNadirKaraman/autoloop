"""`[context] records_dir` — WHERE this loop's context records live (ctx-16).

The setting that ended "thirteen tasks of machinery, no store". Its whole
contract is three claims, and all three are about the LOAD, so this file builds
no repository and runs no git: what a config file means is a pure question.

1. **It is on by default.** An absent `[context]` section, an absent key and the
   example file all name `docs/context`. A store that only exists when somebody
   remembered to configure it is the state this key was added to leave.
2. **It is a REPOSITORY path, not a filesystem one**, and it is normalised
   through the same function the store itself uses — the records are versioned
   and reviewed with the repository they describe, so a value that could not name
   a file IN that repository is not a location.
3. **Only `""` turns it off, and every other unusable value is REFUSED.** An
   absolute path, a `..` segment, a backslash or a run of whitespace cleans to
   the empty string, and accepting that quietly would give a loop that reads no
   records at all while its config file reads as configured — the fail-open the
   context roadmap item exists to close. `""` is compared as the literal, not
   stripped to it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from autoloop.config import (
    DEFAULT_CONTEXT_RECORDS_DIR,
    ContextConfig,
    load_config,
)
from autoloop.context_records import repository_record_store
from autoloop.errors import ConfigError

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.toml"


def write_config(tmp_path: Path, body: str = "") -> Path:
    """A minimal loadable config, plus whatever body is under test — the shape
    `test_config_concurrency.write_config` uses, for its reason."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nworkers_root = "{tmp_path / "w"}"\n\n' + body, encoding="utf-8"
    )
    return path


def records_dir_of(tmp_path: Path, body: str = "") -> str:
    return load_config(write_config(tmp_path, body)).context.records_dir


def test_the_default_is_on_and_is_ctx_02s_directory(tmp_path):
    """1. Three spellings of "I said nothing about it", one answer."""
    assert ContextConfig().records_dir == DEFAULT_CONTEXT_RECORDS_DIR
    assert records_dir_of(tmp_path) == "docs/context"
    assert records_dir_of(tmp_path, "[context]\nmax_records = 25\n") == "docs/context"


def test_the_example_config_names_the_same_directory(tmp_path):
    """1, from the file an operator actually copies. A default that the example
    contradicts is a default nobody runs.

    The example is read as TOML and its `[context]` section is re-loaded through
    the real loader inside a minimal config, the way
    `test_context_resolver.test_the_template_ships_the_section_and_it_loads`
    does — `load_config(EXAMPLE_CONFIG)` itself would be judging the template's
    placeholder `paths`, which is a different claim.
    """
    example = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert tomllib.loads(example)["context"]["records_dir"] == (
        DEFAULT_CONTEXT_RECORDS_DIR
    )
    section = example.split("[context]", 1)[1].split("\n[", 1)[0]
    shipped = load_config(write_config(tmp_path, "[context]" + section + "\n"))
    assert shipped.context.records_dir == DEFAULT_CONTEXT_RECORDS_DIR


def test_a_directory_is_normalised_the_way_a_record_path_is(tmp_path):
    """2. A trailing slash and surrounding space are the same location; the
    normalised form is what `tasks.unauthorized_paths` is later handed."""
    assert records_dir_of(tmp_path, '[context]\nrecords_dir = "docs/context/"\n') == (
        "docs/context"
    )
    assert records_dir_of(tmp_path, '[context]\nrecords_dir = "  context  "\n') == (
        "context"
    )


def test_the_empty_string_is_the_only_way_off(tmp_path):
    """3, the supported half. This is the deployment whose closeout reports
    `no_context_record_store`, and it has to be typed to be got — EXACTLY `""`.
    A run of whitespace cleans to the same `""` the unusable values do and is
    refused with them: it is not a spelling of the switch, and reading it as one
    would be the fail-open the parametrised test below closes for every other
    value."""
    assert records_dir_of(tmp_path, '[context]\nrecords_dir = ""\n') == ""
    assert repository_record_store(tmp_path, "") is None
    for blank in ('"   "', '"\\t"'):
        with pytest.raises(ConfigError) as excinfo:
            records_dir_of(tmp_path, f"[context]\nrecords_dir = {blank}\n")
        assert "context.records_dir" in str(excinfo.value)
        assert "not blank" in str(excinfo.value)


@pytest.mark.parametrize(
    "value",
    [
        '"/srv/records"',       # absolute: names no repository file
        '"../records"',         # escapes the repository
        '"./docs/context"',     # not how git spells it
        '"docs\\\\context"',    # backslash
        "25",                   # a number
        "true",                 # a switch
    ],
)
def test_an_unusable_value_is_refused_and_never_read_as_off(tmp_path, value):
    """3, the fail-open half — the one that matters. Each of these would clean to
    `""`, and `""` means "no records at all": a typo must not be able to turn the
    mechanism off while the file still reads as configured."""
    with pytest.raises(ConfigError) as excinfo:
        records_dir_of(tmp_path, f"[context]\nrecords_dir = {value}\n")
    assert "context.records_dir" in str(excinfo.value)


def test_the_key_is_known_and_a_neighbour_is_still_refused(tmp_path):
    """The section stays strict: adding a key did not open it up."""
    assert records_dir_of(tmp_path, '[context]\nrecords_dir = "records"\n') == "records"
    with pytest.raises(ConfigError):
        records_dir_of(tmp_path, '[context]\nrecord_dir = "records"\n')
