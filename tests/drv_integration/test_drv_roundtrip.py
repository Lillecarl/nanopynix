"""Integration tests for pynixd.drv_parser against real .drv files in the Nix store.

These tests are skipped during normal collection; run them explicitly with:
    pytest tests/drv_integration/ -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from pynixd.drv_parser import parse_drv
from pynixd.operations.query_all_valid_paths import QueryAllValidPathsRequest
from pynixd.store import LocalSocketStore
from tests.conftest import get_test_store_kwargs

log = structlog.get_logger(__name__)


def _get_system_drv_paths() -> list[str]:
    """Query the host store for all .drv paths (synchronous wrapper)."""

    async def _inner() -> list[str]:
        store = LocalSocketStore(
            store_id="system",
            store_path=Path("/"),
            **get_test_store_kwargs(no_probe=True),
        )
        try:
            resp = await store.execute(QueryAllValidPathsRequest())
            return sorted(str(p) for p in resp.paths if p.endswith(".drv"))
        finally:
            await store.close()

    return asyncio.run(_inner())


def pytest_generate_tests(metafunc):
    """Parameterize drv roundtrip tests with real store .drv paths."""
    if "drv_path_str" in metafunc.fixturenames:
        paths = _get_system_drv_paths()
        if not paths:
            pytest.skip("No .drv files found in the Nix store")
        metafunc.parametrize("drv_path_str", paths)


@pytest.mark.timeout(30)
def test_drv_roundtrip(drv_path_str: str) -> None:
    """For a single .drv file: read, parse, serialize, parse, compare."""
    drv_path = Path(drv_path_str)
    raw = drv_path.read_text()
    parsed = parse_drv(raw)
    serialized = parsed.serialize()
    reparsed = parse_drv(serialized)
    assert reparsed.serialize() == serialized
