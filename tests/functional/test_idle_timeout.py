from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from pynixd import Server
from pynixd.store import LocalSocketStore
from tests.conftest import get_test_store_kwargs

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.no_pynixd
@pytest.mark.asyncio
async def test_idle_timeout(tmp_path: Path):
    """Verify that pynixd shuts down after idleness."""
    store_path = tmp_path / "store"
    store_path.mkdir()

    local_store = LocalSocketStore(
        store_id="local",
        store_path=store_path,
        **get_test_store_kwargs(no_probe=True),
    )

    # Low timeout for testing
    server = Server(
        local_store=local_store,
        idle_timeout=2,
        ssh_port=0,
        http_port=0,
    )

    await server.start()
    assert server._started

    # Wait for timeout (2s) + watcher loop
    for _ in range(10):
        if not server._started:
            break
        await asyncio.sleep(1)

    assert not server._started
    assert server.ssh_server is None
