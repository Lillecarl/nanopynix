"""No two test modules of the repository run share a basename.

**This failure appears only in the run that starts at the repository root.**
pytest imports a test module under its basename when no `__init__.py` gives it
a package, so two files called `test_completion.py` in two projects are one
module name. Each project's own gate passes, because a gate run collects one
project and sees one of the two. Every job of the CI test matrix then dies at
collection, before a single test runs.

Issue #222 is the example. `libpynix/tests/test_completion.py` and
`libpynix/tests/test_examples.py` were green in `pytest libpynix`, green in
`checks.libpynix`, and collided with `pynix/tests/test_completion.py` and
`nanopynix/tests/test_examples.py` the moment CI ran the whole thing. That
cost a full matrix run to learn, which is what a self-check is for.

**The roster is the `testpaths` of the repository's own `pytest.ini`, and not
`SUITE_ROOTS`.** Only those directories are collected together, so only they
can collide. `grpclib-transports/tests/test_examples.py` and
`pynix/completions/tests/` are outside it, and each really may reuse a name.
"""

from __future__ import annotations

import configparser
from collections import defaultdict
from pathlib import Path

from tests.support.suite_roots import REPO_ROOT


def _testpaths() -> list[Path]:
    """The directories that the repository run collects, from `pytest.ini`."""
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    return [REPO_ROOT / entry for entry in parser["pytest"]["testpaths"].split()]


def _modules() -> dict[str, list[Path]]:
    """Every collected test module, keyed by the name pytest imports it under."""
    found: dict[str, list[Path]] = defaultdict(list)
    for root in _testpaths():
        for path in sorted(root.rglob("test_*.py")):
            # A package gives its modules a dotted name, so two files under two
            # packages of different names never collide.
            if (path.parent / "__init__.py").is_file():
                continue
            found[path.stem].append(path.relative_to(REPO_ROOT))
    return found


def test_the_scanner_reads_the_real_roster() -> None:
    """The guard: a scanner that found nothing would report success."""
    paths = _testpaths()
    assert len(paths) > 3, f"only {len(paths)} testpath(s) read from pytest.ini"
    missing = [str(p) for p in paths if not p.is_dir()]
    assert not missing, f"pytest.ini names {missing}, and no such directory exists"
    assert len(_modules()) > 100, "the glob found almost no test module; the pattern is wrong"


def test_no_two_collected_test_modules_share_a_name() -> None:
    """A collision fails collection for every job of the CI matrix."""
    clashing = {name: paths for name, paths in _modules().items() if len(paths) > 1}

    assert not clashing, (
        "these test module names appear more than once in the repository run, and pytest imports "
        "each under its basename, so collection fails before any test runs:\n"
        + "\n".join(f"  {name}: {', '.join(str(p) for p in paths)}" for name, paths in sorted(clashing.items()))
        + "\nRename one of each pair. A project gate collects one project and would not report this."
    )
