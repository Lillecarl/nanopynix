"""
asyncssh SSH server that speaks the nix-daemon protocol.

Accepts SSH connections, authenticates, and spawns a DaemonProxy
for each `nix-daemon --stdio` exec request.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import asyncssh

from . import wire
from .build_queue import BuildQueue
from .gc import GarbageCollector
from .local_store_db import LocalStoreDB
from .proxy import DaemonProxy
from .scheduler import Scheduler
from .store import Store
from .wire import SSHNixReader, SSHNixWriter

log: logging.Logger = logging.getLogger(__name__)


class _NixSSHServer(asyncssh.SSHServer):
    """Accept all authentication (development mode)."""

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        log.info("Password auth: user=%s", username)
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        log.info("Pubkey auth: user=%s key=%s", username, key.get_fingerprint())
        return True


async def run_server(
    stores: dict[str, Store],
    local_store: Store,
    host: str = "127.0.0.1",
    port: int = 0,
    host_key_path: str | None = None,
    ready_event: asyncio.Event | None = None,
    port_holder: list[int] | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Run the SSH server until cancelled.

    Args:
        stores: Store instances (shared across clients)
        local_store: Shared local Store for client connections
        host: Listen address
        port: Listen port (0 for random available port)
        host_key_path: Path to SSH host key
        ready_event: Set when server is ready
        port_holder: If provided, appends the bound port
    """
    # Load or generate host key
    if host_key_path and os.path.exists(host_key_path):
        host_key: asyncssh.SSHKey = asyncssh.read_private_key(host_key_path)
        log.info("Loaded host key from %s", host_key_path)
    else:
        host_key = asyncssh.generate_private_key("ssh-rsa", key_size=4096)
        if host_key_path:
            host_key.write_private_key(host_key_path)
            log.info("Generated and saved host key to %s", host_key_path)
        else:
            log.info("Generated ephemeral host key")

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

    # Shared build queue and scheduler
    build_queue = BuildQueue()
    scheduler = Scheduler(
        build_queue=build_queue,
        stores=stores,
        local_store=local_store,
    )

    async def handle_client(process: asyncssh.SSHServerProcess) -> None:
        cmd: str | None = process.command
        log.info("Client exec: %s", cmd)

        if not cmd or ("nix-daemon" not in cmd and "nix daemon" not in cmd):
            process.stderr.write(b"pynixd: unsupported command\n")
            process.exit(1)
            return

        process.channel.set_write_buffer_limits(
            high=wire._SSH_WINDOW_SIZE, low=wire._SSH_WINDOW_SIZE // 4
        )
        exit_code = 0
        try:
            proxy = DaemonProxy(
                SSHNixReader(process.stdin),
                SSHNixWriter(process.stdout),
                local_store=local_store,
                build_queue=build_queue,
                scheduler_trigger=scheduler.trigger,
            )
            await proxy.run()
        except Exception:
            log.exception("Proxy session failed")
            exit_code = 1
        finally:
            process.exit(exit_code)

    # Start the SSH server
    log.info("About to call asyncssh.listen on %s:%d", host, port)
    async with await asyncssh.listen(
        host,
        port,
        server_host_keys=[host_key],
        server_factory=_NixSSHServer,
        process_factory=handle_client,
        encoding=None,
    ) as server:
        log.info("asyncssh.listen __aenter__ complete, server: %s", server)
        log.info("server.get_addresses(): %s", server.get_addresses())
        bound_port = server.get_port()
        log.info("pynixd SSH server listening on %s:%d", host, bound_port)
        if port_holder is not None:
            port_holder.append(bound_port)
        if ready_event:
            ready_event.set()
        # Start scheduler, DB flush, and GC tasks
        scheduler_task = asyncio.create_task(scheduler.start())
        gc: GarbageCollector | None = None
        if local_store.db is not None:
            local_store.db.start()
            gc = GarbageCollector(
                db=local_store.db,
                stores=stores,
                local_store=local_store,
            )
            gc.start()
        try:
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
            if gc is not None:
                await gc.stop()
            if local_store.db is not None:
                await local_store.db.close()
