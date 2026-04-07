"""Core pynixd instance orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh
import structlog

if TYPE_CHECKING:
    from aiohttp import web

from .gc import GarbageCollector
from .http_cache import BinaryCacheServer
from .local_store_db import LocalStoreDB
from .scheduler import Scheduler
from .ssh_server import start_ssh_server
from .store import Store
from .unix_server import start_unix_server

log = structlog.get_logger(__name__)


class NixImplementation(Enum):
    NIX = auto()
    LIX = auto()


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
        if self.config.stores:
            self.scheduler: Scheduler | None = Scheduler(
                self.config.stores, self.config.local_store
            )
        else:
            self.scheduler = None

        self.background_tasks: list[asyncio.Task[Any]] = []
        self.ssh_server: asyncssh.SSHAcceptor | None = None
        self.unix_server: asyncio.Server | None = None
        self.http_server: web.AppRunner | None = None
        self.https_server: web.AppRunner | None = None

    @property
    def host(self) -> str:
        """SSH listen host."""
        return self.config.ssh_host

    @property
    def port(self) -> int:
        """SSH listen port (actual port if 0 was passed)."""
        if self.ssh_server and self.ssh_server.sockets:
            return self.ssh_server.sockets[0].getsockname()[1]
        return self.config.ssh_port or 0

    @property
    def username(self) -> str:
        """SSH username."""
        import os

        return os.environ.get("USER", "root")

    def uri(self, implementation: NixImplementation = NixImplementation.NIX) -> str:
        """ssh-ng:// URI for --store."""
        username = self.username
        match implementation:
            case NixImplementation.NIX:
                return f"ssh-ng://{username}@{self.host}:{self.port}"
            case NixImplementation.LIX:
                return f"ssh-ng://{username}@{self.host}?port={self.port}"

        return f"ssh-ng://{username}@{self.host}:{self.port}"

    def builder_uri(
        self,
        max_jobs: int = 4,
        implementation: NixImplementation = NixImplementation.NIX,
    ) -> str:
        """Builder spec for --builders."""
        from .store import get_current_system

        system = get_current_system()
        return f"{self.uri(implementation)} {system} - {max_jobs}"

    def uri_for(
        self, uri_format: str, implementation: NixImplementation = NixImplementation.NIX
    ) -> str:
        """Return URI in the given format."""
        if uri_format == "ssh-ng":
            return self.uri(implementation)
        elif uri_format == "unix":
            # For unix-server tests
            return f"unix://{self.config.unix_path}" if self.config.unix_path else ""
        return self.uri(implementation)

    async def __aenter__(self) -> Server:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
        await self.wait_finished()

    async def start(self) -> None:
        """Start the server listeners and background tasks."""
        local_store = self.config.local_store
        stores = self.config.stores

        await local_store.probe_version()
        local_store.db = await LocalStoreDB.open(local_store.store_path or Path("/"))

        for store in stores.values():
            from .operations.queries import QueryAllValidPathsRequest

            try:
                await store.execute(QueryAllValidPathsRequest())
            except Exception:
                log.exception("sync_paths_failed", id=store.id)

        # Start background services
        if self.scheduler:
            scheduler_task = asyncio.create_task(self.scheduler.start())
            self.background_tasks.append(scheduler_task)

        if local_store.db:
            local_store.db.start()
            gc = GarbageCollector(local_store.db, stores, local_store)
            gc.start()
            if gc.task:
                self.background_tasks.append(gc.task)

        # Start listeners
        if self.config.ssh_port is not None:
            self.ssh_server = await start_ssh_server(
                stores=stores,
                local_store=local_store,
                scheduler=self.scheduler,
                host=self.config.ssh_host,
                port=self.config.ssh_port,
                host_key_path=self.config.ssh_host_key,
            )

        if self.config.unix_path:
            self.unix_server = await start_unix_server(
                stores=stores,
                local_store=local_store,
                scheduler=self.scheduler,
                socket_path=self.config.unix_path,
            )

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
                self.http_server = runner
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
                self.https_server = runner

        if not (
            self.ssh_server or self.unix_server or self.http_server or self.https_server
        ):
            log.warning("no_servers_started")

    async def wait_finished(self) -> None:
        """Wait for the server listeners to close."""
        wait_tasks = []
        if self.ssh_server:
            wait_tasks.append(asyncio.create_task(self.ssh_server.wait_closed()))
        if self.unix_server:
            wait_tasks.append(asyncio.create_task(self.unix_server.wait_closed()))

        if wait_tasks:
            await asyncio.gather(*wait_tasks)
        elif self.http_server or self.https_server:
            # Only HTTP runners, wait forever (until cancelled)
            while True:
                await asyncio.sleep(3600)

    async def close(self) -> None:
        """Gracefully shut down the server."""
        log.info("server_shutting_down")
        if self.http_server:
            await self.http_server.cleanup()
        if self.https_server:
            await self.https_server.cleanup()

        if self.ssh_server:
            self.ssh_server.close()
        if self.unix_server:
            self.unix_server.close()

        # Stop scheduler gracefully (cancels builds etc)
        if self.scheduler:
            await self.scheduler.stop()

        for task in self.background_tasks:
            task.cancel()

        local_store = self.config.local_store
        if local_store.db:
            await local_store.db.close()

        await local_store.close()
        for store in self.config.stores.values():
            await store.close()
