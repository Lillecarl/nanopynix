"""Core pynixd instance orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

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
    ssh_host_key: Path | None = None

    # Unix Server
    unix_path: Path | None = None

    # HTTP Binary Cache
    http_host: str = "0.0.0.0"
    http_port: int | None = None
    http_user: str | None = None
    http_pass: str | None = None
    http_priority: int = 30

    # HTTPS Binary Cache
    https_port: int | None = None
    https_cert: Path | None = None
    https_key: Path | None = None


class Server:
    """Programmatic pynixd server instance."""

    def __init__(self, config: PynixdConfig | dict | None = None, **kwargs) -> None:
        if isinstance(config, dict):
            # Merge dict with kwargs to instantiate PynixdConfig
            merged = {**config, **kwargs}
            if not merged.get("local_store"):
                from .store import LocalSocketStore

                merged["local_store"] = LocalSocketStore(
                    id="local", store_path=Path("/")
                )
            config = PynixdConfig(**merged)
        elif config is None:
            # If nothing is passed, use kwargs
            if not kwargs.get("local_store"):
                from .store import LocalSocketStore

                kwargs["local_store"] = LocalSocketStore(
                    id="local", store_path=Path("/")
                )
            config = PynixdConfig(**kwargs)

        self.config: PynixdConfig = config
        self.build_queue = BuildQueue()
        self.scheduler = Scheduler(
            self.build_queue, self.config.stores, self.config.local_store
        )
        self.background_tasks: list[asyncio.Task] = []
        self.servers: list[asyncssh.SSHAcceptor | asyncio.Server] = []
        self.http_runners: list[asyncio.Task] = []

    async def start(self) -> None:
        """Start the server listeners and background tasks."""
        local_store = self.config.local_store
        stores = self.config.stores

        await local_store.probe_version()
        local_store.db = await LocalStoreDB.open(local_store.store_path or Path("/"))

        for store in stores.values():
            try:
                await store.sync_paths()
            except Exception:
                log.exception("Failed to sync paths for store %s", store.id)

        # Start background services
        scheduler_task = asyncio.create_task(self.scheduler.start())
        self.background_tasks.append(scheduler_task)

        if local_store.db:
            local_store.db.start()
            gc = GarbageCollector(local_store.db, stores, local_store)
            gc.start()
            if gc._task:
                self.background_tasks.append(gc._task)

        # Start listeners
        if self.config.ssh_port is not None:
            ssh_server = await start_ssh_server(
                stores=stores,
                local_store=local_store,
                build_queue=self.build_queue,
                scheduler=self.scheduler,
                host=self.config.ssh_host,
                port=self.config.ssh_port,
                host_key_path=self.config.ssh_host_key,
            )
            self.servers.append(ssh_server)

        if self.config.unix_path:
            unix_server = await start_unix_server(
                stores=stores,
                local_store=local_store,
                build_queue=self.build_queue,
                scheduler=self.scheduler,
                socket_path=self.config.unix_path,
            )
            self.servers.append(unix_server)

        if self.config.http_port is not None or self.config.https_port is not None:
            cache = BinaryCacheServer(
                local_store,
                username=self.config.http_user,
                password=self.config.http_pass,
                priority=self.config.http_priority,
            )
            if self.config.http_port is not None:
                runner, _ = await cache.start(
                    host=self.config.http_host,
                    port=self.config.http_port,
                )
                self.http_runners.append(runner)
            if self.config.https_port is not None:
                runner, _ = await cache.start(
                    host=self.config.http_host,
                    port=self.config.https_port,
                    ssl_cert=str(self.config.https_cert)
                    if self.config.https_cert
                    else None,
                    ssl_key=str(self.config.https_key)
                    if self.config.https_key
                    else None,
                )
                self.http_runners.append(runner)

        if not self.servers and not self.http_runners:
            log.warning("No servers started! Check your configuration.")

    async def wait_finished(self) -> None:
        """Wait for the server listeners to close."""
        wait_tasks = []
        for s in self.servers:
            if hasattr(s, "wait_closed"):
                wait_tasks.append(asyncio.create_task(s.wait_closed()))

        if wait_tasks:
            await asyncio.gather(*wait_tasks)
        elif self.http_runners:
            # Only HTTP runners, wait forever (until cancelled)
            while True:
                await asyncio.sleep(3600)

    async def close(self) -> None:
        """Gracefully shut down the server."""
        log.info("Shutting down pynixd Server...")
        for runner in self.http_runners:
            if hasattr(runner, "cleanup"):
                await runner.cleanup()
        for s in self.servers:
            s.close()

        # Stop scheduler gracefully (cancels builds etc)
        await self.scheduler.stop()

        for task in self.background_tasks:
            task.cancel()

        local_store = self.config.local_store
        if local_store.db:
            await local_store.db.close()

        await local_store.close()
        for store in self.config.stores.values():
            await store.close()


async def run_pynixd(
    config: PynixdConfig,
    ready_event: asyncio.Event | None = None,
    servers_callback: Callable[[list[asyncssh.SSHAcceptor | asyncio.Server]], None]
    | None = None,
) -> list[asyncssh.SSHAcceptor | asyncio.Server]:
    """Run a pynixd instance with the given configuration (legacy shim).

    Args:
        config: Instance configuration.
        ready_event: Set when all servers are listening.
        servers_callback: Optional callback called with list of started servers.

    Returns:
        List of started server/acceptor instances.
    """
    server = Server(config)
    try:
        await server.start()

        if servers_callback:
            servers_callback(server.servers)
        if ready_event:
            ready_event.set()

        await server.wait_finished()
        return server.servers

    except asyncio.CancelledError:
        log.info("Shutting down pynixd instance (cancelled)...")
        return server.servers
    finally:
        await server.close()
