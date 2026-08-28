"""Drop the tests a narrowed round does not need, by node id.

Loaded with `-p autoloop.pytest_deselect --autoloop-deselect <file>`, which
`validation.select_validation_commands` adds when `[audit] test_selection` is
`per_test`. Without the option it does nothing at all, so the plugin being
loaded is not by itself a change in behaviour.

**Why a file and an option rather than a longer command.** A narrowed round
deselects on the order of a thousand tests, and `--deselect` once per test would
be a 60KB argv reproduced in the transcript, the evidence line and every
reviewer's screen. **Why not an environment variable**: `run_validation_commands`
builds the subprocess environment EXPLICITLY, as the parent's minus an
allowlist, so a name it does not know about does not survive to pytest.

**Why the list is of what to DROP, not what to keep.** A keep-list would make
every test the analysis failed to name — a shape nobody modelled, a
parametrisation whose id does not match — disappear from the run silently. A
drop-list can only ever remove tests something positively decided were
unnecessary; anything unrecognised simply runs. The fail-open direction is the
whole point, and it has to be structural rather than remembered.
"""

from __future__ import annotations

from pathlib import Path

#: `<test file, repo-relative>::<function name>`, one per line. Parametrised
#: cases are named WITHOUT their `[...]` id: the analysis reasons about a test
#: function, and every case of one shares its dependencies.
OPTION = "--autoloop-deselect"


def pytest_addoption(parser) -> None:
    parser.addoption(
        OPTION,
        action="store",
        default="",
        metavar="PATH",
        help=(
            "File of `path::name` lines to deselect. Absent or empty, nothing "
            "is deselected. Written by autoloop's per-test validation selection."
        ),
    )


def _entries(path: Path) -> frozenset[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Unreadable list: run everything. The alternative is deciding what to
        # skip from a file we could not read.
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


def pytest_collection_modifyitems(config, items) -> None:
    option = config.getoption(OPTION, default="")
    if not option:
        return
    drop = _entries(Path(option))
    if not drop:
        return
    keep, dropped = [], []
    for item in items:
        # `location[0]` is the file relative to rootdir, which is what the
        # selection names. `originalname` is the undecorated function name for a
        # parametrised item; `name` carries the `[case]` suffix.
        rel = item.location[0]
        name = getattr(item, "originalname", None) or item.name.split("[")[0]
        (dropped if f"{rel}::{name}" in drop else keep).append(item)
    if dropped:
        items[:] = keep
        config.hook.pytest_deselected(items=dropped)
