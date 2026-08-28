#!/usr/bin/env python3
"""Assert that per-test dependencies never claim LESS than a test really ran.

`autoloop.per_test_deps` decides statically which modules one test can reach.
If that answer is ever missing a module the test actually executed, a narrowed
round would skip a test that does exercise the change — and report success. That
failure is silent by construction: nothing raises, the suite passes, and the
selection is simply wrong.

So the static answer is checked against reality. Run the suite under per-test
coverage contexts, then run this: for every test, the statically derived module
set, CLOSED OVER THE IMPORT GRAPH, must be a SUPERSET of the modules coverage
saw execute.

    COVERAGE_FILE=.coverage-contexts python3 -m pytest autoloop/tests \\
        --cov=autoloop --cov-context=test --cov-report= -q -n auto -p no:cacheprovider
    python3 scripts/check_selection_soundness.py .coverage-contexts

Coverage is the CHECK, never the mechanism — see `per_test_deps`'s docstring for
why a recorded map must not decide what runs. Contexts survive `-n auto`, which
is worth knowing: serially this takes over an hour, in parallel about ten
minutes.

Exit codes:
    0  every modelled test is sound, or opaque and therefore always selected
    1  at least one test would have been narrowed away wrongly
    2  the check could not run (no database, no contexts, unreadable)

A test coverage recorded but this cannot model — a class-based or generated one
the analysis does not name — is REPORTED AND COUNTED, never silently passed. A
growing `unmodelled` count is the warning that the analysis is drifting behind
the suite it is supposed to describe.
"""

import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoloop.per_test_deps import dependencies_by_test  # noqa: E402
from autoloop.validation import build_import_graph  # noqa: E402

#: Coverage records a context per test PHASE (`...|setup`, `...|run`). The node
#: id is what precedes the bar, and parametrised cases carry `[...]` the static
#: analysis does not distinguish.
_PHASE = "|"


def _forward_edges(graph) -> dict[str, set[str]]:
    """`{file: everything it imports, directly}`. `ImportGraph` stores the
    REVERSE edge (who imports whom), which is what selection needs; the check
    needs the other direction, because naming a module means executing what it
    imports."""
    forward: dict[str, set[str]] = {}
    for target, importers in graph.importers.items():
        for importer in importers:
            forward.setdefault(importer, set()).add(target)
    return forward


def _closed(modules, forward) -> set[str]:
    seen, frontier = set(modules), list(modules)
    while frontier:
        for nxt in forward.get(frontier.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


def _executed_per_test(database: Path, root: Path) -> dict[str, set[str]]:
    connection = sqlite3.connect(database)
    files = {i: p for i, p in connection.execute("select id, path from file")}
    contexts = {i: c for i, c in connection.execute("select id, context from context")}
    ran: dict[str, set[str]] = {}
    for file_id, context_id in connection.execute(
        "select distinct file_id, context_id from line_bits"
    ):
        context = contexts.get(context_id, "")
        if not context:
            continue  # module import and other work outside any test
        try:
            rel = Path(files[file_id]).resolve().relative_to(root).as_posix()
        except ValueError:
            continue  # outside the checkout: stdlib, site-packages
        if rel.startswith("autoloop/") and not rel.startswith("autoloop/tests/"):
            ran.setdefault(context.split(_PHASE)[0], set()).add(rel)
    return ran


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    database = Path(argv[1])
    if not database.exists():
        print(f"no coverage database at {database}", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[1]
    graph = build_import_graph(root)
    forward = _forward_edges(graph)
    ran_by_test = _executed_per_test(database, root)
    if not ran_by_test:
        print(
            "the database holds no per-test contexts — was it produced with "
            "`--cov-context=test`?",
            file=sys.stderr,
        )
        return 2

    analysed: dict[str, dict] = {}
    checked = opaque = unmodelled = 0
    violations: list[tuple[str, list[str]]] = []
    missed_counts: Counter = Counter()

    for node, ran in sorted(ran_by_test.items()):
        path, _, name = node.partition("::")
        if not path.endswith(".py") or not name:
            continue
        name = name.split("[")[0]  # a parametrised case is one test here
        if path not in analysed:
            try:
                analysed[path] = dependencies_by_test(root, path, graph.files)
            except (OSError, SyntaxError, ValueError):
                analysed[path] = {}
        deps = analysed[path].get(name)
        if deps is None:
            unmodelled += 1
            continue
        checked += 1
        if deps.opaque:
            opaque += 1
            continue  # always selected: it cannot be narrowed away
        missing = ran - _closed(deps.modules, forward)
        if missing:
            violations.append((node, sorted(missing)))
            missed_counts.update(missing)

    print(f"tests checked      : {checked}")
    print(f"  opaque (always selected): {opaque}")
    print(f"  not modelled            : {unmodelled}")
    print(f"violations         : {len(violations)}")
    if not violations:
        print("\nSOUND: every modelled test's static module set covers what it ran.")
        return 0

    print("\nmodules the static answer missed most often:")
    for module, count in missed_counts.most_common(10):
        print(f"  {count:5d}  {module}")
    print("\nfirst failures:")
    for node, missing in violations[:10]:
        print(f"  {node}")
        print(f"      missing: {', '.join(missing[:5])}")
    print(
        "\nUNSOUND: each of these tests executed a module the analysis does not "
        "attribute to it, so a narrowed round would skip it wrongly. Fix "
        "`autoloop/per_test_deps.py` — never the expectation."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
