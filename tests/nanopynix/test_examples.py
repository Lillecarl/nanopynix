"""Run doc examples as integration tests to prevent staleness."""

from __future__ import annotations

import asyncio
import runpy
import sys
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"
if not _EXAMPLES.is_dir():
    pytest.skip("examples directory not found", allow_module_level=True)

_EXAMPLE_FILES = sorted(_EXAMPLES.glob("*_example.py"))

# Dynamic primop registration (including the built-in YAML primops) is broken
# on Nix 2.31 and isn't expected to be fixed there — see nanopynix.primops.
_requires_dynamic_primops = pytest.mark.nix_capability("dynamic_primop_registration")
_EXAMPLE_PARAMS = [
    pytest.param(path, marks=_requires_dynamic_primops) if path.name == "primops_example.py" else path
    for path in _EXAMPLE_FILES
]


@pytest.mark.parametrize("path", _EXAMPLE_PARAMS, ids=lambda p: p.name)
def test_example_runs(path: Path) -> None:
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
