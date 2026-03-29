"""
Unix socket server that speaks the nix-daemon protocol.

Accepts connections on a Unix domain socket and spawns a DaemonProxy
for each client. Used for testing (avoids SSH) and local daemon mode.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from . import wire
from .build_queue import BuildQueue
from .local_store_db import LocalStoreDB
from .proxy import DaemonProxy
from .scheduler import Scheduler
from .store import Store
from .wire import UnixNixReader, UnixNixWriter

log: logging.Logger = logging.getLogger(__name__)


async def run_unix_server(
    stores: dict[str, Store],
    local_store: Store,
    socket_path: str,
    ready_event: asyncio.Event | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Run a Unix socket server until cancelled.

    Args:
        stores: Store instances (shared across clients)
        local_store: Shared local Store for client connections
        socket_path: Path for the Unix domain socket
        ready_event: Set when server is ready
    """
    # Probe local store version before accepting clients
    await local_store.probe_version()
    log.info("Local store protocol version: %s", wire.proto_str(local_store.version))

    # Open direct SQLite access to the local store (optional — graceful fallback)
    local_store.db = await LocalStoreDB.open(local_store.store_path or "/")

    # Sync known paths on each store at startup
    for store in stores.values():
        try:
            await store.sync_paths()
        except Exception:
            log.exception("Failed to sync paths for store %s", store.id)

    build_queue = BuildQueue()
    scheduler = Scheduler(
        build_queue=build_queue,
        stores=stores,
        local_store=local_store,
    )

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername") or "unknown"
        log.info("Unix client connected: %s", peer)

        try:
            proxy = DaemonProxy(
                UnixNixReader(reader),
                UnixNixWriter(writer),
                local_store=local_store,
                build_queue=build_queue,
                scheduler_trigger=scheduler.trigger,
            )
            await proxy.run()
        except Exception:
            log.exception("Unix proxy session failed")
        finally:
            writer.close()

    # Clean up stale socket
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    server = await asyncio.start_unix_server(handle_client, path=socket_path)

    log.info("pynixd Unix server listening on %s", socket_path)
    if ready_event:
        ready_event.set()

    # Start scheduler and DB flush tasks
    scheduler_task = asyncio.create_task(scheduler.start())
    if local_store.db is not None:
        local_store.db.start()

    try:
        async with server:
            while True:
                if stop_event is not None and stop_event.is_set():
                    server.close()
                    break
                try:
                    await asyncio.wait_for(server.wait_closed(), timeout=0.1)
                    break
                except TimeoutError:
                    continue
    except asyncio.CancelledError:
        server.close()
        await server.wait_closed()
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        if local_store.db is not None:
            await local_store.db.close()
