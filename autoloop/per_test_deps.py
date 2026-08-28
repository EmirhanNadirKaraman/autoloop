"""Which repository modules can ONE TEST reach, decided statically.

`validation.select_validation_commands` narrows a round to whole test FILES,
because imports are a file-level fact. Measured on this checkout, that narrows
very little: a change to `autoloop/dashboard.py` selects 72 of 93 test files and
a change to `autoloop/git_gateway.py` selects 88. Most of the tests in those
files never touch the changed module — they are in the same file as one that
does.

This module answers the finer question without running anything: for each test
function, the set of repository modules it can reach, from its own name
references closed over the helpers, classes and fixtures of its own file. The
caller then closes THAT over the import graph exactly as it does for a file.

**Why static, when a coverage database would be easier.** The soundness of every
narrowed run rests on a graph that can be reasoned about and shown to be
complete (`conftest.py` says so at length about its own edges). A recorded
coverage map is stale the moment code moves, and its failure mode is silent
UNDER-selection — running too few tests and reporting success. Nothing here
records anything; it is derived from the source on every call.

**Coverage is the CHECK, never the mechanism.**
`scripts/check_selection_soundness.py` runs the suite under per-test coverage
contexts and asserts this module's answer is a SUPERSET of what actually ran.
That gate found four defects the analysis itself reported as fine, every one of
them a silent under-selection:

  * a helper imported from a SIBLING TEST MODULE (`from test_implement_executor
    import run_git`) — `autoloop/tests` has no `__init__.py`, so it is on
    `sys.path` and such a module is named without any package prefix;
  * a CLASS that builds the wiring in `__init__` (`RealWiring(tmp_path)`),
    where only functions were being followed;
  * an AUTOUSE FIXTURE, which runs for every test in its module while nothing
    in any test's own text points at it;
  * and, in the gate itself, comparing this module's answer without first
    closing it over the import graph.

A fifth will arrive with the next test written in a shape nobody modelled, which
is why the gate runs in CI rather than having been run once.

**Everything unresolvable is OPAQUE, and an opaque test is always selected** —
the same fail-open shape `validation._scan_module` uses for whole files. Being
wrong by running too MANY tests is a cost; being wrong the other way is a lie.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: Directories on `sys.path` when pytest runs, so a module in one is imported by
#: its BARE name. `autoloop/tests` qualifies because it has no `__init__.py`.
BARE_NAME_DIRS = ("autoloop/tests", "")

#: Calls whose FIRST ARGUMENT may be a string naming a module —
#: `monkeypatch.setattr("autoloop.orchestrator.foo", ...)`. The module is named,
#: as a literal, so it is read rather than given up on: the same move
#: `validation._names_document` makes on document names in code strings.
_STRING_TARGET_CALLS = frozenset({"setattr", "delattr", "patch", "object"})

#: Names that reach code by a route no reader can follow.
_OPAQUE_NAMES = frozenset({"importlib", "getfixturevalue", "__import__", "eval", "exec"})


@dataclass(frozen=True)
class TestDeps:
    """What one test can reach, and whether that answer is trustworthy."""

    #: Repo-relative modules the test names, directly or through its file.
    modules: frozenset[str]
    #: True when something could not be resolved. An opaque test must be
    #: selected for EVERY change; callers may not narrow it away.
    opaque: bool


class PerTestAnalysis:
    """Per-test dependencies for one test file, against a known file set.

    `files` is the import graph's file set (`ImportGraph.files`), used to decide
    whether a dotted name is repository code at all. Nothing is cached across
    instances: the tree is read once per file, per call.
    """

    def __init__(self, root: Path, rel: str, files: frozenset[str]):
        self._files = files
        self._rel = rel
        source = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        self._bindings: dict[str, set[str]] = {}
        self._unresolved = False
        self._read_imports(tree)
        self._defs: dict[str, ast.AST] = {}
        for node in ast.walk(tree):
            # Classes are followed WHOLE: `RealWiring(tmp_path)` names the class
            # and what its `__init__` reaches is what the test reaches. Methods
            # are indexed by bare name because a call site says `wiring.build()`
            # and the receiver's type is not known here. Both over-approximate,
            # which only ever ADDS modules.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._defs.setdefault(node.name, node)
        self._autouse = self._read_autouse(tree)

    # ---- resolution ---------------------------------------------------------

    def _to_rel(self, dotted: str) -> str | None:
        stem = dotted.replace(".", "/")
        for prefix in BARE_NAME_DIRS:
            base = f"{prefix}/{stem}" if prefix else stem
            for candidate in (base + ".py", base + "/__init__.py"):
                if candidate in self._files:
                    return candidate
        return None

    def _could_be_repo_code(self, dotted: str) -> bool:
        """Would a repository module plausibly be spelled this way? `autoloop.x`
        always could; a bare name could be a sibling test module. Anything else
        is stdlib or third-party and is correctly ignored rather than feared."""
        head = dotted.split(".")[0]
        return head == "autoloop" or f"autoloop/tests/{head}.py" in self._files

    def _read_imports(self, tree) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    rel = self._to_rel(alias.name)
                    if rel:
                        name = alias.asname or alias.name.split(".")[0]
                        self._bindings.setdefault(name, set()).add(rel)
                    elif self._could_be_repo_code(alias.name):
                        self._unresolved = True
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # a relative import inside the tests tree
                    self._unresolved = True
                    continue
                module = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        self._unresolved = True
                        continue
                    rel = self._to_rel(f"{module}.{alias.name}") or self._to_rel(module)
                    if rel:
                        self._bindings.setdefault(alias.asname or alias.name, set()).add(rel)
                    elif self._could_be_repo_code(module):
                        # Looks like repository code and did not resolve. Losing
                        # it silently is how a test stops naming a module it
                        # really uses, so the whole file fails open instead.
                        self._unresolved = True

    @staticmethod
    def _read_autouse(tree) -> frozenset[str]:
        """Fixtures declared `autouse=True`.

        They run for every test in the module WITHOUT being named as a
        parameter, so no test's own text points at one. The edge does not exist
        unless it is added here.
        """
        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "fixture":
                    continue
                for keyword in decorator.keywords:
                    if (
                        keyword.arg == "autouse"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value
                    ):
                        found.add(node.name)
        return frozenset(found)

    # ---- one definition -----------------------------------------------------

    def _direct(self, node) -> tuple[set[str], set[str], bool]:
        """`(modules named, definitions reached, opaque)` for one definition."""
        modules: set[str] = set()
        reached: set[str] = set()
        opaque = self._unresolved
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                continue  # already in `_bindings`: `_read_imports` walked the tree
            if isinstance(sub, ast.Name):
                if sub.id in _OPAQUE_NAMES:
                    opaque = True
                modules |= self._bindings.get(sub.id, set())
                if sub.id in self._defs:
                    reached.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                if sub.attr in _OPAQUE_NAMES:
                    opaque = True
                # `wiring.publish()` is a method call on something this file
                # built; the receiver's type is unknown, so any definition of
                # that name here is followed.
                if sub.attr in self._defs:
                    reached.add(sub.attr)
            elif isinstance(sub, ast.Call):
                func = sub.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in _STRING_TARGET_CALLS and sub.args:
                    first = sub.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        rel = self._longest_module_prefix(first.value)
                        if rel:
                            modules.add(rel)
                    elif not isinstance(first, (ast.Name, ast.Attribute)):
                        opaque = True  # the patch target is computed
        arguments = getattr(node, "args", None)
        if arguments is not None:
            # A fixture arrives as a PARAMETER name.
            for argument in list(arguments.args) + list(arguments.kwonlyargs):
                if argument.arg in self._defs:
                    reached.add(argument.arg)
        return modules, reached, opaque

    def _longest_module_prefix(self, dotted: str) -> str | None:
        """`autoloop.orchestrator.Orchestrator._x` -> `autoloop/orchestrator.py`."""
        parts = dotted.split(".")
        while parts:
            rel = self._to_rel(".".join(parts))
            if rel:
                return rel
            parts.pop()
        return None

    # ---- the whole file -----------------------------------------------------

    def tests(self) -> dict[str, TestDeps]:
        """`{test function name: TestDeps}` for every `test_*` in the file.

        Iterative rather than recursive: following classes whole makes the call
        graph deep and cyclic — a helper builds an object whose methods call
        helpers — and a recursive closure overflowed the stack on
        `test_stale_record_rebuild.py`.
        """
        direct = {name: self._direct(node) for name, node in self._defs.items()}

        def close(seed: tuple[set[str], set[str], bool]) -> TestDeps:
            modules, frontier, opaque = set(seed[0]), set(seed[1]) | set(self._autouse), seed[2]
            seen: set[str] = set()
            while frontier:
                current = frontier.pop()
                if current in seen or current not in direct:
                    continue
                seen.add(current)
                found, reached, was_opaque = direct[current]
                modules |= found
                opaque |= was_opaque
                frontier |= reached - seen
            return TestDeps(frozenset(modules), opaque)

        return {
            name: close(direct[name])
            for name in self._defs
            if name.startswith("test_")
        }


def dependencies_by_test(root: Path, rel: str, files: frozenset[str]) -> dict[str, TestDeps]:
    """`{test name: TestDeps}` for one test file. See `PerTestAnalysis`.

    NOT named `test_...`: pytest collects any such name in a module it imports,
    so the helper became a failing "test" in the suite that imports it.
    """
    return PerTestAnalysis(root, rel, files).tests()
