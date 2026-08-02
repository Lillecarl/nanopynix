"""Run doc examples as integration tests to prevent staleness."""

from __future__ import annotations

import asyncio
import runpy
import sys
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples"
_EXAMPLE_FILES = [
    pytest.param(p.name, marks=pytest.mark.tcp if "tcp" in p.name else ())
    for p in sorted(_EXAMPLES.glob("*_example.py"))
]


@pytest.mark.parametrize("name", _EXAMPLE_FILES)
def test_example_runs(name: str) -> None:
    """Run each example script in its own event loop."""
    path = _EXAMPLES / name
    loop = asyncio.new_event_loop()
    sys.path.insert(0, str(_EXAMPLES))
    try:
        asyncio.set_event_loop(loop)
        runpy.run_path(str(path), run_name="__main__")
    finally:
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        sys.path.remove(str(_EXAMPLES))
