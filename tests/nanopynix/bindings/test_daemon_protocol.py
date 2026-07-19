"""The Python socket layer can delegate Nix's daemon protocol to C++."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest

import nanopynix


async def _serve_one_connection(listener: socket.socket, store: Any) -> None:
    connection, _address = await asyncio.to_thread(listener.accept)
    try:
        await asyncio.to_thread(
            nanopynix.process_connection,
            store,
            connection.fileno(),
            trusted=False,
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_process_connection_serves_nix_daemon_protocol(tmp_path: Path) -> None:
    socket_path = tmp_path / "daemon.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()
    store = nanopynix.open_store(f"local?root={tmp_path / 'store'}")
    handler_task = asyncio.create_task(_serve_one_connection(listener, store))

    try:
        process = await asyncio.create_subprocess_exec(
            "nix",
            "store",
            "ping",
            "--store",
            f"unix://{socket_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        await handler_task
    finally:
        listener.close()
        if not handler_task.done():
            handler_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handler_task
        store.close()
