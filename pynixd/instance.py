"""Core pynixd instance orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import asyncssh

from .build_queue import BuildQueue
from .gc import GarbageCollector
from .http_cache import BinaryCacheServer
from .local_store_db import LocalStoreDB
from .scheduler import Scheduler
from .ssh_server import start_ssh_server
from .store import Store
from .unix_server import start_unix_server

log = logging.getLogger(__name__)


@dataclass
class PynixdConfig:
    """Configuration for a pynixd instance."""

    local_store: Store
    stores: Mapping[str, Store] = field(default_factory=dict)

    # SSH Server
    ssh_host: str = "127.0.0.1"
    ssh_port: int | None = None  # None to disable, 0 for random
    ssh_host_key: str | None = None

    # Unix Server
    unix_path: str | None = None

    # HTTP Binary Cache
    http_host: str = "0.0.0.0"
    http_port: int | None = None
    http_user: str | None = None
    http_pass: str | None = None
    http_priority: int = 30

    # HTTPS Binary Cache
    https_port: int | None = None
    https_cert: str | None = None
    https_key: str | None = None


async def run_pynixd(
    config: PynixdConfig,
    ready_event: asyncio.Event | None = None,
    servers_callback: Callable[[list[asyncssh.SSHAcceptor | asyncio.Server]], None]
    | None = None,
) -> list[asyncssh.SSHAcceptor | asyncio.Server]:
    """Run a pynixd instance with the given configuration.

    Args:
        config: Instance configuration.
        ready_event: Set when all servers are listening.
        servers_callback: Optional callback called with list of started servers.

    Returns:
        List of started server/acceptor instances.
    """
    local_store = config.local_store
    stores = config.stores

    # Shared resources
    build_queue = BuildQueue()
    scheduler = Scheduler(build_queue, stores, local_store)

    # Initialize local store and backends
    await local_store.probe_version()
    local_store.db = await LocalStoreDB.open(local_store.store_path or "/")

    for store in stores.values():
        try:
            await store.sync_paths()
        except Exception:
            log.exception("Failed to sync paths for store %s", store.id)

    background_tasks = []
    servers = []
    http_runners = []

    try:
        # Start background services
        scheduler_task = asyncio.create_task(scheduler.start())
        background_tasks.append(scheduler_task)

        if local_store.db:
            local_store.db.start()
            gc = GarbageCollector(local_store.db, stores, local_store)
            # Use gc.start() which creates a task
            gc.start()
            if gc._task:
                background_tasks.append(gc._task)

        # Start listeners
        if config.ssh_port is not None:
            ssh_server = await start_ssh_server(
                stores=stores,
                local_store=local_store,
                build_queue=build_queue,
                scheduler=scheduler,
                host=config.ssh_host,
                port=config.ssh_port,
                host_key_path=config.ssh_host_key,
            )
            servers.append(ssh_server)

        if config.unix_path:
            unix_server = await start_unix_server(
                stores=stores,
                local_store=local_store,
                build_queue=build_queue,
                scheduler=scheduler,
                socket_path=config.unix_path,
            )
            servers.append(unix_server)

        if config.http_port is not None or config.https_port is not None:
            cache = BinaryCacheServer(
                local_store,
                username=config.http_user,
                password=config.http_pass,
                priority=config.http_priority,
            )
            if config.http_port is not None:
                runner, _ = await cache.start(
                    host=config.http_host,
                    port=config.http_port,
                )
                http_runners.append(runner)
            if config.https_port is not None:
                runner, _ = await cache.start(
                    host=config.http_host,
                    port=config.https_port,
                    ssl_cert=config.https_cert,
                    ssl_key=config.https_key,
                )
                http_runners.append(runner)

        if not servers and not http_runners:
            log.warning("No servers started! Check your configuration.")
            if servers_callback:
                servers_callback(servers)
            if ready_event:
                ready_event.set()
            return []

        if servers_callback:
            servers_callback(servers)

        if ready_event:
            ready_event.set()

        # Wait for all servers to close
        wait_tasks = []
        for s in servers:
            if hasattr(s, "wait_closed"):
                wait_tasks.append(asyncio.create_task(s.wait_closed()))

        if wait_tasks:
            await asyncio.gather(*wait_tasks)
        else:
            # Only HTTP runners, wait forever (until cancelled)
            while True:
                await asyncio.sleep(3600)

        return servers

    except asyncio.CancelledError:
        log.info("Shutting down pynixd instance...")
        return servers
    finally:
        for runner in http_runners:
            await runner.cleanup()
        for s in servers:
            s.close()

        # Stop scheduler gracefully (cancels builds etc)
        await scheduler.stop()

        for task in background_tasks:
            task.cancel()

        if local_store.db:
            await local_store.db.close()

        await local_store.close()
        for store in stores.values():
            await store.close()
