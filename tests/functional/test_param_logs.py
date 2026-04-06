"""Tests for parameterized log file naming."""

from __future__ import annotations

import pytest

from tests.conftest import run_captured


@pytest.mark.parametrize("value", [1, 2, 3])
async def test_param(value: int) -> None:
    """Parameterized test to verify separate log files."""
    rc, stdout, _ = await run_captured(["echo", str(value)])
    assert rc == 0
    assert stdout.strip() == str(value)
