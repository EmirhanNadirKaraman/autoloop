"""Build a test's git repository by copying one, not by running `git init`.

**Nothing here may import `autoloop`**, for the reason `conftest.py` gives at
length about itself: every edge this module has becomes an edge each of its
importers has, and `validation.select_validation_commands` reads exactly that
graph to decide which test files a commit's changed paths can reach. Stdlib
only, so importing this says "this file builds a git repository" and no more.

WHY A TEMPLATE AT ALL. A repository costs six git subprocesses — `init`, three
`config`, `add`, `commit` — and 158.8ms measured on this machine. The suite
built 2,206 of them per run: 19,614 of its 49,210 git spawns and roughly 350
CPU seconds spent on setup that no test is asserting about. Copying a prebuilt
one costs 11.7ms, so the same repository arrives 13.6x faster. The cost is
fork/exec, not disk — `GIT_TEMPLATE_DIR` pointed at an empty directory, which
skips git's fourteen sample hooks, was measured at 1.03x and abandoned.

WHY A SHARED INITIAL SHA IS NOT A NEW HAZARD. Every repository copied from one
key carries that template's initial commit, so they all share its sha. That is
not a change this introduced: a git commit timestamp has ONE-SECOND
granularity, so two repositories built back to back from identical content by
the same author ALREADY produced byte-identical commit objects — verified on
this checkout, two independent builds and one on a different branch all giving
`4b820069`, because a branch is a ref and never enters the commit. What changed
is that the collision is now deterministic rather than depending on which side
of a second boundary two builds happened to fall.

WORKER-LOCAL BY CONSTRUCTION. The cache lives under a directory this PROCESS
created, so xdist workers neither share one nor race to build it — the reason
it is not a fixed path under `/tmp`. One template per key per worker, amortised
over every repository that worker copies from it.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

#: The identity most builders in this suite configured for themselves. Kept as
#: the default so a converted builder keeps the commits it always made: author
#: and committer are part of the commit object, so changing one changes the sha.
DEFAULT_EMAIL = "test@example.com"
DEFAULT_NAME = "Test"

#: `(("README.md", "hello\n"),)` — what the majority of builders committed.
DEFAULT_FILES: tuple[tuple[str, str], ...] = (("README.md", "hello\n"),)

_CACHE_ROOT: Path | None = None
_TEMPLATES: dict[tuple, Path] = {}


def run_git(cwd, *args, **kwargs) -> str:
    """One git command in `cwd`, checked, stdout returned.

    The signature every converted builder already had, so a file that needs a
    git command this module does not model keeps using its own.
    """
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **kwargs
    ).stdout


def _cache_root() -> Path:
    global _CACHE_ROOT
    if _CACHE_ROOT is None:
        _CACHE_ROOT = Path(tempfile.mkdtemp(prefix="autoloop-gitrepo-"))
        atexit.register(shutil.rmtree, _CACHE_ROOT, True)
    return _CACHE_ROOT


def _template(key: tuple, branch: str, files, message: str, email: str, name: str) -> Path:
    template = _TEMPLATES.get(key)
    if template is not None:
        return template
    template = _cache_root() / f"template-{len(_TEMPLATES)}"
    template.mkdir(parents=True)
    run_git(template, "init", "-q", "-b", branch)
    run_git(template, "config", "user.email", email)
    run_git(template, "config", "user.name", name)
    run_git(template, "config", "commit.gpgsign", "false")
    for rel, body in files:
        path = template / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    run_git(template, "add", "-A")
    run_git(template, "commit", "-q", "-m", message)
    _TEMPLATES[key] = template
    return template


def make_repo_from_template(
    dest,
    *,
    branch: str = "main",
    files: Sequence[tuple[str, str]] = DEFAULT_FILES,
    message: str = "init",
    email: str = DEFAULT_EMAIL,
    name: str = DEFAULT_NAME,
) -> Path:
    """A git repository at `dest` holding one commit, copied from a template.

    Equivalent to `init -b <branch>`, the three `config` calls every builder
    made, writing `files`, `add -A` and `commit -m <message>` — but only the
    FIRST call for a given key pays for that. `dest` may already exist; its
    parents are created.
    """
    dest = Path(dest)
    key = (branch, tuple(files), message, email, name)
    template = _template(key, branch, files, message, email, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, dest, symlinks=True, dirs_exist_ok=True)
    return dest
