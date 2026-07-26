"""Gate: the checked-in CLI reference matches the live pynix command tree.

``docs/pynix/reference.md`` is generated, and ``docs/conf.py`` regenerates it
on every Sphinx build -- but only into the *rendered site*. The copy in the
repository refreshes only when a human runs the generator and commits, and
nothing checked that they had. Measured: it was 252 lines stale, missing
``pynix lsp``, ``pynix osearch``, ``pynix ekn`` and two ``pynix build`` flags
outright.

This compares rendered-now against checked-in and does not write, so a stale
file is a red test rather than a surprise diff in someone else's commit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR = _REPO_ROOT / "docs" / "_generate_pynix_reference.py"
_REFERENCE = _REPO_ROOT / "docs" / "pynix" / "reference.md"


def _load_generator() -> ModuleType:
    """Import the generator by path -- ``docs/`` is not a package."""
    spec = importlib.util.spec_from_file_location("_generate_pynix_reference", _GENERATOR)
    if spec is None or spec.loader is None:
        pytest.fail(f"could not load the generator at {_GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_checked_in_cli_reference_is_current() -> None:
    rendered = _load_generator().render()
    checked_in = _REFERENCE.read_text()
    assert rendered == checked_in, (
        "docs/pynix/reference.md is stale -- regenerate it with "
        "`python docs/_generate_pynix_reference.py` and commit the result"
    )
