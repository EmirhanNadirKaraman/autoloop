"""Per-test dependency analysis: what one test can reach, decided statically.

Fixture-only and deliberately so — every claim here is about reading source, so
a real repository or a subprocess would be dead weight (see CLAUDE.md, Writing
tests). The one thing these CANNOT establish is soundness against a real suite;
`scripts/check_selection_soundness.py` does that, in its own CI job, by running
the tests and comparing. Four of the cases below exist because that gate caught
the analysis being wrong in exactly that way.
"""

from __future__ import annotations

from pathlib import Path

from autoloop.per_test_deps import dependencies_by_test

#: A file set standing in for the import graph's. Only membership matters.
FILES = frozenset(
    {
        "autoloop/orchestrator.py",
        "autoloop/tasks.py",
        "autoloop/note_merge.py",
        "autoloop/tests/helper_module.py",
        "autoloop/tests/test_subject.py",
    }
)


def analyse(tmp_path: Path, body: str, rel: str = "autoloop/tests/test_subject.py"):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return dependencies_by_test(tmp_path, rel, FILES)


def test_a_test_naming_a_module_reaches_it(tmp_path):
    found = analyse(tmp_path, """
from autoloop.tasks import Task

def test_one():
    Task(id="t1")
""")
    assert found["test_one"].modules == frozenset({"autoloop/tasks.py"})
    assert not found["test_one"].opaque


def test_a_test_that_names_nothing_reaches_nothing(tmp_path):
    """The whole point: a file may import twenty modules while THIS test uses
    none of them, and that is what makes narrowing possible at all."""
    found = analyse(tmp_path, """
from autoloop.tasks import Task
from autoloop.orchestrator import Orchestrator

def test_arithmetic():
    assert 1 + 1 == 2
""")
    assert found["test_arithmetic"].modules == frozenset()
    assert not found["test_arithmetic"].opaque


def test_a_helper_in_the_same_file_carries_its_modules(tmp_path):
    found = analyse(tmp_path, """
from autoloop.tasks import Task

def build():
    return Task(id="t1")

def test_uses_helper():
    build()
""")
    assert found["test_uses_helper"].modules == frozenset({"autoloop/tasks.py"})


def test_a_sibling_test_module_is_a_real_edge(tmp_path):
    """`autoloop/tests` has no `__init__.py`, so it is on `sys.path` and one test
    module borrows another's helpers by BARE name. Resolving only repo-relative
    paths missed these, and the whole helper's reach went with them — 105
    soundness violations, the first thing the gate found."""
    found = analyse(tmp_path, """
from helper_module import make

def test_borrows():
    make()
""")
    assert found["test_borrows"].modules == frozenset({"autoloop/tests/helper_module.py"})
    assert not found["test_borrows"].opaque


def test_a_class_is_followed_whole(tmp_path):
    """`RealWiring(tmp_path)` builds the world in `__init__`. Collecting only
    functions left the name matching nothing, so everything the constructor
    reached was missed."""
    found = analyse(tmp_path, """
from autoloop.orchestrator import Orchestrator

class Wiring:
    def __init__(self):
        self.orchestrator = Orchestrator()

def test_builds_wiring():
    Wiring()
""")
    assert found["test_builds_wiring"].modules == frozenset({"autoloop/orchestrator.py"})


def test_a_method_call_on_a_built_object_is_followed(tmp_path):
    """`t.publish(...)` names a method, not a bare name. The receiver's type is
    unknown here, so any definition of that name in the file is followed."""
    found = analyse(tmp_path, """
from autoloop.note_merge import resolve_note_append

class Wiring:
    def sweep(self):
        return resolve_note_append()

def build():
    return Wiring()

def test_sweeps():
    t = build()
    t.sweep()
""")
    assert found["test_sweeps"].modules == frozenset({"autoloop/note_merge.py"})


def test_an_autouse_fixture_reaches_every_test_in_the_module(tmp_path):
    """Nothing in a test's own text points at an autouse fixture — it runs for
    every test in the module regardless. The edge does not exist unless the
    analysis adds it, and the test that proved it imported only `config`."""
    found = analyse(tmp_path, """
import pytest
from autoloop.orchestrator import Orchestrator

@pytest.fixture(autouse=True)
def _register():
    Orchestrator()
    yield

def test_names_nothing_itself():
    assert True
""")
    assert found["test_names_nothing_itself"].modules == frozenset({"autoloop/orchestrator.py"})


def test_a_fixture_requested_by_name_is_followed(tmp_path):
    found = analyse(tmp_path, """
import pytest
from autoloop.tasks import Task

@pytest.fixture
def task():
    return Task(id="t1")

def test_uses_fixture(task):
    assert task
""")
    assert found["test_uses_fixture"].modules == frozenset({"autoloop/tasks.py"})


def test_a_string_patch_target_names_its_module(tmp_path):
    """`monkeypatch.setattr("autoloop.orchestrator.x", ...)` NAMES the module, as
    a literal. Treating `monkeypatch` as dynamic instead made 272 tests opaque
    for no reason."""
    found = analyse(tmp_path, """
def test_patches(monkeypatch):
    monkeypatch.setattr("autoloop.orchestrator.Orchestrator.run", lambda self: None)
""")
    assert found["test_patches"].modules == frozenset({"autoloop/orchestrator.py"})
    assert not found["test_patches"].opaque


def test_an_unresolvable_repository_import_is_opaque(tmp_path):
    """Fail open. A name that looks like repository code and resolves to nothing
    is the shape in which a test quietly stops naming what it uses."""
    found = analyse(tmp_path, """
from autoloop.does_not_exist import thing

def test_one():
    thing()
""")
    assert found["test_one"].opaque


def test_a_star_import_is_opaque(tmp_path):
    found = analyse(tmp_path, """
from autoloop.tasks import *

def test_one():
    assert True
""")
    assert found["test_one"].opaque


def test_a_computed_patch_target_is_opaque(tmp_path):
    found = analyse(tmp_path, """
def test_one(monkeypatch):
    monkeypatch.setattr("autoloop." + "orchestrator.x", None)
""")
    assert found["test_one"].opaque


def test_a_dynamic_import_is_opaque(tmp_path):
    found = analyse(tmp_path, """
import importlib

def test_one():
    importlib.import_module("autoloop.orchestrator")
""")
    assert found["test_one"].opaque
