"""Run each documented example, so a change to the library cannot leave it stale.

``docs/libpynix/index.md`` points at these files rather than repeating them,
for the reason ``docs/grpclib-transports/index.md`` gives: a script the suite
runs breaks loudly, and a snippet written into a page does not break at all.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
NAMES = sorted(path.name for path in EXAMPLES.glob("*_example.py"))


def test_the_roster_is_not_empty() -> None:
    """The guard: a glob that matched nothing would report success."""
    assert NAMES, f"no example under {EXAMPLES}"


@pytest.mark.parametrize("name", NAMES)
def test_an_example_runs(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Each example runs as ``__main__``, which is how a reader runs it."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        runpy.run_path(str(EXAMPLES / name), run_name="__main__")
    finally:
        sys.path.remove(str(EXAMPLES))

    assert capsys.readouterr().out.strip(), f"{name} printed nothing"
