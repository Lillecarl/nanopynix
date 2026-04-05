"""
Unix socket server that speaks the nix-daemon protocol.

Accepts connections on a Unix domain socket and spawns a DaemonProxy
for each client. Used for testing (avoids SSH) and local daemon mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import structlog

from .build_queue import BuildQueue
from .proxy import DaemonProxy
from .scheduler import Scheduler
from .store import Store
from .wire import UnixNixReader, UnixNixWriter

log = structlog.get_logger(__name__)


async def start_unix_server(
    stores: Mapping[str, Store],
    local_store: Store,
    build_queue: BuildQueue | None,
    scheduler: Scheduler | None,
    socket_path: Path,
) -> asyncio.Server:
    """Start a Unix socket server.

    Args:
        stores: Store instances (shared across clients)
        local_store: Shared local Store for client connections
        build_queue: Shared build queue (None in local mode)
        scheduler: Shared scheduler (None in local mode)
        socket_path: Path for the Unix domain socket

    Returns:
        The asyncio.Server instance.
    """

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername") or "unknown"
        log.info("unix_client_connected", peer=peer)
        try:
            proxy = DaemonProxy(
                UnixNixReader(reader),
                UnixNixWriter(writer),
                local_store=local_store,
                build_queue=build_queue,
                scheduler_trigger=scheduler.trigger if scheduler else None,
            )
            await proxy.run()
        except Exception:
            log.exception("unix_proxy_session_failed")
        finally:
            writer.close()

    # Clean up stale socket
    if socket_path.exists():
        socket_path.unlink()

    server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
    log.info("unix_server_listening", socket_path=socket_path)
    return server
