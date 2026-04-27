"""
Unix socket server that speaks the nix-daemon protocol.

Accepts connections on a Unix domain socket and spawns a DaemonProxy
for each client. Used for testing (avoids SSH) and local daemon mode.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from .config import ScheduleMode
from .operations.base import Role
from .proxy import DaemonProxy
from .wire import UnixNixReader, UnixNixWriter

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .scheduler import Scheduler
    from .store import Store

log = structlog.get_logger(__name__)


async def start_unix_server(
    stores: Mapping[str, Store],  # noqa: ARG001
    local_store: Store,
    scheduler: Scheduler | None,
    socket_path: Path,
    schedule_mode: ScheduleMode | None = None,
) -> asyncio.Server:
    """Start a Unix socket server.

    Args:
        stores: Store instances (shared across clients)
        local_store: Shared local Store for client connections
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
                UnixNixReader(reader, identifier="client"),
                UnixNixWriter(writer, identifier="client"),
                local_store=local_store,
                scheduler=scheduler,
                role=Role.ADMIN,
                username="local",
                schedule_mode=schedule_mode or ScheduleMode.auto,
            )
            await proxy.run()
        except Exception:
            log.exception("unix_proxy_session_failed")
        finally:
            writer.close()

    # Clean up stale socket
    if socket_path.exists():  # noqa: ASYNC240
        socket_path.unlink()  # noqa: ASYNC240

    server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
    log.info("unix_server_listening", socket_path=socket_path)
    return server
