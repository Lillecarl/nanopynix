"""Core pynixd instance orchestration."""

from __future__ import annotations

import asyncio
import os
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh
import structlog

if TYPE_CHECKING:
    from aiohttp import web

from . import wire
from .config import PynixdSettings
from .gc import GarbageCollector
from .http_server import PynixdHttpServer
from .local_store_db import LocalStoreDB
from .path_tracker import PathTracker
from .scheduler import Scheduler
from .ssh_server import start_ssh_server
from .store import LocalSocketStore, Store, get_current_system
from .unix_server import start_unix_server

log = structlog.get_logger(__name__)


class NixImplementation(Enum):
    NIX = auto()
    LIX = auto()


class Server:
    """Programmatic pynixd server instance.

    Accepts either a ``PynixdSettings`` object (from config file + env vars)
    or individual kwargs for programmatic/test use.
    """

    def __init__(
        self,
        local_store: Store | None = None,
        stores: dict[str, Store] | None = None,
        settings: PynixdSettings | None = None,
        **kwargs: Any,
    ) -> None:
        if local_store is None:
            local_store = LocalSocketStore(id="local", store_path=Path("/"))
        if stores is None:
            stores = {}

        self.local_store: Store = local_store
        self.stores: dict[str, Store] = stores
        self.settings: PynixdSettings = settings or PynixdSettings(**kwargs)

        self.scheduler: Scheduler | None = Scheduler(self.stores, self.local_store)

        self.background_tasks: list[asyncio.Task[Any]] = []
        self.ssh_server: asyncssh.SSHAcceptor | None = None
        self.unix_server: asyncio.Server | None = None
        self.http_server: web.AppRunner | None = None
        self.http_bound_port: int | None = None
        self.https_server: web.AppRunner | None = None
        self.https_bound_port: int | None = None
        self.path_tracker: PathTracker = PathTracker(db=None)
        self._started = False

    async def add_store(self, store: Store) -> None:
        """Add a remote store to the server, linking it to the central DB and path tracker."""
        from .operations.query_all_valid_paths import QueryAllValidPathsRequest

        await store.probe()

        local_store = self.local_store
        store.db = local_store.db
        store.tracker = self.path_tracker.get_instance(store.id, is_local=False)

        if local_store.db is not None:
            paths = await local_store.db.get_known_paths(store.id)
            if paths:
                store.tracker.add_known_paths(paths, update_regtime=False)
                log.info("loaded_cached_paths", store_id=store.id, count=len(paths))

        self.stores[store.id] = store

        if self.scheduler:
            self.scheduler.add_store(store.id, store)

        try:
            await store.execute(QueryAllValidPathsRequest())
        except Exception:
            log.exception("sync_paths_failed", id=store.id)

    async def remove_store(self, store_id: str, drain_timeout: float = 300.0) -> None:
        """Remove a remote store, cleaning DB records and closing connections."""
        if self.scheduler:
            await self.scheduler.remove_store(store_id, drain_timeout=drain_timeout)
        
        # Scheduler.remove_store already popped it from allocator/stores,
        # but we also pop from self.stores for consistency.
        store = self.stores.pop(store_id, None)
        if store is None:
            log.warning("remove_store_not_found", store_id=store_id)
            return

        local_store = self.local_store
        if local_store.db is not None:
            await local_store.db.remove_store_paths(store_id)

        # Scheduler.remove_store already closed it
        log.info("removed_store", store_id=store_id)

    @property
    def host(self) -> str:
        return self.settings.ssh_host

    @property
    def port(self) -> int:
        if self.ssh_server and self.ssh_server.sockets:
            return self.ssh_server.sockets[0].getsockname()[1]
        return self.settings.ssh_port or 0

    @property
    def username(self) -> str:
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
        system = get_current_system()
        return f"{self.uri(implementation)} {system} - {max_jobs}"

    def uri_for(
        self, uri_format: str, implementation: NixImplementation = NixImplementation.NIX
    ) -> str:
        """Return URI in the given format."""
        if uri_format == "ssh-ng":
            return self.uri(implementation)
        elif uri_format == "unix":
            if not self.settings.unix_path:
                return ""
            uri = f"unix://{self.settings.unix_path}"
            uri += f"?root={self.local_store.store_path}"
            return uri
        return self.uri(implementation)

    async def __aenter__(self) -> Server:
        await self.start()
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Start the server listeners and background tasks."""
        if self._started:
            raise RuntimeError("Server already started")
        self._started = True
        local_store = self.local_store
        stores = self.stores

        await local_store.probe()

        if local_store.version < wire.proto(1, 35):
            raise RuntimeError(
                f"Local store {local_store.id} uses protocol {wire.proto_str(local_store.version)}, "
                "but pynixd requires at least 1.35 for the local store. "
                "Please upgrade your Nix daemon."
            )

        if local_store.db_enabled:
            local_store.db = await LocalStoreDB.open(
                local_store.store_path or Path("/")
            )
            self.path_tracker.db = local_store.db
        else:
            local_store.db = None
            self.path_tracker.db = None

        local_store.tracker = self.path_tracker.get_instance(
            local_store.id, is_local=True
        )

        stores_to_add = list(self.stores.values())
        self.stores.clear()

        for store in stores_to_add:
            await store.probe()
            await self.add_store(store)

        if self.scheduler:
            scheduler_task = asyncio.create_task(self.scheduler.start())
            self.background_tasks.append(scheduler_task)

        if local_store.db:
            local_store.db.start()
            gc = GarbageCollector(local_store.db, stores, local_store)
            gc.start()
            if gc.task:
                self.background_tasks.append(gc.task)

        s = self.settings
        if s.ssh_port is not None:
            self.ssh_server = await start_ssh_server(
                stores=stores,
                local_store=local_store,
                scheduler=self.scheduler,
                host=s.ssh_host,
                port=s.ssh_port,
                host_key_path=s.ssh_host_key,
                admin_users=s.admin_users,
            )

        if s.unix_path:
            self.unix_server = await start_unix_server(
                stores=stores,
                local_store=local_store,
                scheduler=self.scheduler,
                socket_path=s.unix_path,
            )

        if s.http_port is not None or s.https_port is not None:
            cache = PynixdHttpServer(
                local_store,
                enable_cache=s.http_enable_cache,
                enable_metrics=s.http_enable_metrics,
                metrics_no_auth=s.http_metrics_no_auth,
                username=s.http_user,
                password=s.http_pass,
                htpasswd_path=s.http_htpasswd,
                priority=s.http_priority,
                upload_dir=s.http_upload_dir,
            )
            if s.http_port is not None:
                runner, port = await cache.start(
                    host=s.http_host,
                    port=s.http_port,
                )
                self.http_server = runner
                self.http_bound_port = port
            if s.https_port is not None:
                runner, port = await cache.start(
                    host=s.http_host,
                    port=s.https_port,
                    ssl_cert=str(s.https_cert) if s.https_cert else None,
                    ssl_key=str(s.https_key) if s.https_key else None,
                )
                self.https_server = runner
                self.https_bound_port = port

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
            while True:
                await asyncio.sleep(3600)

    async def close(self) -> None:
        """Gracefully shut down the server."""
        if not self._started:
            raise RuntimeError("Server not started or already closed")
        self._started = False
        log.info("server_shutting_down")
        if self.http_server:
            await self.http_server.cleanup()
        if self.https_server:
            await self.https_server.cleanup()

        if self.ssh_server:
            self.ssh_server.close()
        if self.unix_server:
            self.unix_server.close()

        if self.scheduler:
            await self.scheduler.stop()

        for task in self.background_tasks:
            task.cancel()

        local_store = self.local_store
        if local_store.db:
            await local_store.db.close()

        await local_store.close()
        for store in self.stores.values():
            await store.close()
