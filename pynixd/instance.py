"""Core pynixd instance orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Mapping

    import asyncssh
    from aiohttp import web

from . import wire
from .config import PynixdSettings
from .context import PynixdContext
from .gc import GarbageCollector
from .http_server import PynixdHttpServer
from .local_store_db import LocalStoreDB
from .path_tracker import PathTracker
from .scheduler import Scheduler
from .ssh_server import start_ssh_server
from .store import LocalSocketStore, Store
from .types.ids import StoreId
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
        stores: dict[StoreId, Store] | None = None,
        settings: PynixdSettings | None = None,
        **kwargs: Any,
    ) -> None:
        if local_store is None:
            local_store = LocalSocketStore(store_id=StoreId("local"), store_path=Path("/"))
        if stores is None:
            stores = {}

        settings = settings or PynixdSettings(**kwargs)
        path_tracker = PathTracker(db=None)

        self.ctx = PynixdContext(
            settings=settings,
            local_store=local_store,
            _stores=stores,
            path_tracker=path_tracker,
        )

        self.ctx.scheduler = Scheduler(self.ctx)

        self.background_tasks: list[asyncio.Task[Any]] = []
        self.ssh_server: asyncssh.SSHAcceptor | None = None
        self.unix_server: asyncio.Server | None = None
        self.http_server: web.AppRunner | None = None
        self.http_bound_port: int | None = None
        self.https_server: web.AppRunner | None = None
        self.https_bound_port: int | None = None
        self._started = False
        self._last_activity_at: float = time.monotonic()

    @property
    def local_store(self) -> Store:
        return self.ctx.local_store

    @property
    def stores(self) -> Mapping[StoreId, Store]:
        return self.ctx.stores

    @property
    def settings(self) -> PynixdSettings:
        return self.ctx.settings

    @property
    def scheduler(self) -> Scheduler | None:
        return self.ctx.scheduler

    @property
    def path_tracker(self) -> PathTracker:
        return self.ctx.path_tracker

    def record_activity(self) -> None:
        """Update last activity timestamp."""
        now = time.monotonic()
        self._last_activity_at = now
        if self.ctx.scheduler:
            self.ctx.scheduler.record_activity()

    async def _idleness_watcher(self) -> None:
        """Monitor idleness and shutdown if timeout reached."""
        if not self.ctx.settings.idle_timeout:
            return

        timeout = float(self.ctx.settings.idle_timeout)
        log.info("idleness_watcher_started", timeout=timeout)

        while True:
            try:
                await asyncio.sleep(1)
                now = time.monotonic()

                # Activity from BuildQueue
                last_activity = self._last_activity_at
                if self.ctx.scheduler:
                    last_activity = max(
                        last_activity,
                        self.ctx.scheduler.last_activity_at,
                    )

                    pending = self.ctx.scheduler.queue.count(status="pending")
                    running = self.ctx.scheduler.queue.count(status="running")
                    if pending > 0 or running > 0:
                        self.record_activity()
                        last_activity = now

                if now - last_activity > timeout:
                    log.info(
                        "idle_timeout_reached",
                        idle_for=round(now - last_activity, 1),
                    )
                    # Signal application exit by closing servers
                    # We do this in a separate task to avoid blocking the watcher
                    _close_task = asyncio.create_task(self.close())
                    _close_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                    self.background_tasks.append(_close_task)
                    break
            except Exception:
                log.exception("idle_watcher_error")
                await asyncio.sleep(5)

    async def add_store(self, store: Store, dynamic: bool = False) -> None:
        """Add a store to the server, setting up path tracking for scheduling.

        If dynamic=True, the store's feature_matrix is also registered in
        the scheduler's dynamic_feature_matrix, so builds for that platform
        continue to queue even after the store is removed.

        Stores already configured with a tracker (e.g. local_store) keep
        theirs. Remote stores get a path tracker instance linked to the
        central DB so the scheduler knows which paths they have.
        """
        if store.tracker.parent is None:
            store.tracker = self.path_tracker.create_instance(store.store_id, is_local=False)

        local_store = self.local_store
        if local_store.db is not None and store.tracker.parent is not None:
            paths = await local_store.db.get_known_paths(store.store_id)
            if paths:
                store.tracker.add_known_paths(paths, update_regtime=False)
                log.info("loaded_cached_paths", store_id=store.store_id, count=len(paths))

        await store.start()

        # Server is the primary owner and mutator of the stores collection
        self.ctx._stores[store.store_id] = store

        if self.scheduler:
            self.scheduler.on_store_added(store, dynamic=dynamic)

    async def remove_store(self, store_id: StoreId, drain_timeout: float = 300.0) -> None:
        """Remove a remote store, cleaning DB records and closing connections.

        NOTE: Used by external projects — do not remove.
        """
        if self.scheduler:
            # First, drain the store in the scheduler to cancel/requeue jobs
            await self.scheduler.drain_store(store_id, drain_timeout=drain_timeout)

        # Then remove from the context
        store = self.ctx._stores.pop(store_id, None)
        if store is None:
            log.warning("remove_store_not_found", store_id=store_id)
            return

        local_store = self.local_store
        if local_store.db is not None:
            try:
                async with local_store.db.acquire_conn() as conn:
                    await conn.execute(
                        "DELETE FROM PynixdKnownPaths WHERE storeId = ?",
                        (str(store_id),),
                    )
                    await conn.commit()
                    log.info("removed_store_path_data", store_id=store_id)
            except Exception:
                log.warning(
                    "remove_store_db_cleanup_failed",
                    store_id=store_id,
                    exc_info=True,
                )

        # Finally, close the store connection
        await store.close()

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
        local_store = self.ctx.local_store

        await local_store.start()

        if local_store.version < wire.proto(1, 35):
            raise RuntimeError(
                f"Local store {local_store.store_id} uses protocol {wire.proto_str(local_store.version)}, "
                "but pynixd requires at least 1.35 for the local store. "
                "Please upgrade your Nix daemon.",
            )

        if local_store.db_enabled:
            self.ctx.db = await LocalStoreDB.open(local_store.store_path or Path("/"))
            local_store.db = self.ctx.db
            self.ctx.path_tracker.db = self.ctx.db
        else:
            self.ctx.db = None
            local_store.db = None
            self.ctx.path_tracker.db = None

        local_store.tracker = self.ctx.path_tracker.create_instance(
            local_store.store_id,
            is_local=True,
        )

        if self.ctx.db:
            self.ctx.db.start()

        # Gather stores to add and then clear the context mapping to ensure
        # add_store (which populates it) starts from a clean state for these IDs.
        stores_to_add = list(self.ctx.stores.values())
        self.ctx._stores.clear()

        if stores_to_add:
            async with asyncio.TaskGroup() as tg:
                for s in stores_to_add:
                    tg.create_task(self.add_store(s))

        if self.ctx.scheduler:
            self.background_tasks.append(
                asyncio.create_task(self.ctx.scheduler.start()),
            )

        if self.ctx.db:
            gc = GarbageCollector(self.ctx)
            self.background_tasks.append(asyncio.create_task(gc.run()))

        s = self.settings
        if s.ssh_port is not None:
            self.ssh_server = await start_ssh_server(
                ctx=self.ctx,
                host=s.ssh_host,
                port=s.ssh_port,
                host_key_path=s.ssh_host_key,
                admin_users=s.admin_users,
                schedule_mode=s.schedule_mode,
            )

        if s.unix_path:
            self.unix_server = await start_unix_server(
                ctx=self.ctx,
                socket_path=s.unix_path,
                schedule_mode=s.schedule_mode,
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

        if self.settings.idle_timeout:
            self.background_tasks.append(asyncio.create_task(self._idleness_watcher()))

        if not (self.ssh_server or self.unix_server or self.http_server or self.https_server):
            log.warning("no_servers_started")

    async def close(self) -> None:
        """Gracefully shut down the server."""
        if not self._started:
            return
        self._started = False
        log.info("server_shutting_down")

        if self.http_server:
            await self.http_server.cleanup()
            self.http_server = None
        if self.https_server:
            await self.https_server.cleanup()
            self.https_server = None

        if self.ssh_server:
            self.ssh_server.close()
            await self.ssh_server.wait_closed()
            self.ssh_server = None
        if self.unix_server:
            self.unix_server.close()
            await self.unix_server.wait_closed()
            self.unix_server = None

        if self.ctx.db:
            await self.ctx.db.close()
            self.ctx.db = None

        if self.ctx.scheduler:
            await self.ctx.scheduler.close()

        for task in self.background_tasks:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        self.background_tasks.clear()

        await self.local_store.close()
        for store in self.stores.values():
            await store.close()
        self.ctx._stores.clear()
