"""Core pynixd instance orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import os
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import anyio
import structlog

from . import wire
from .config import LocalSocketStoreSpec, PynixdSettings
from .context import PynixdContext
from .http_server import PynixdHttpServer
from .local_store_db import LocalStoreDB
from .operations.pynixd_collect_garbage import PynixdCollectGarbageRequest
from .path_tracker import PathTracker
from .reverse_client import ReverseInitiator
from .reverse_server import start_reverse_acceptor
from .scheduler import Scheduler
from .ssh_server import start_ssh_server
from .stderr import OperationLogs
from .store import DaemonStore, LocalDBStore, LocalStore
from .substitution import HttpBinaryCacheSubstituter, SubstitutionManager
from .types import PynixdGCAction
from .types.ids import StoreId
from .unix_server import start_unix_server

if TYPE_CHECKING:
    from collections.abc import Mapping

    import asyncssh
    from aiohttp import web

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
        stores: dict[StoreId, DaemonStore] | None = None,
        settings: PynixdSettings | None = None,
        **kwargs: Any,
    ) -> None:
        settings = settings or PynixdSettings(**kwargs)

        if stores is None:
            spec = LocalSocketStoreSpec(store_id=StoreId("local"), monitor=False)
            stores = {StoreId("local"): spec.to_store(str(StoreId("local")))}
        elif StoreId("local") not in stores:
            stores = dict(stores)
            spec = LocalSocketStoreSpec(store_id=StoreId("local"), monitor=False)
            stores[StoreId("local")] = spec.to_store(str(StoreId("local")))

        path_tracker = PathTracker(db=None)

        self.ctx = PynixdContext(
            settings=settings,
            _stores=stores,
            substitution_manager=SubstitutionManager([HttpBinaryCacheSubstituter("https://cache.nixos.org/")]),
            path_tracker=path_tracker,
        )

        self.ctx.scheduler = Scheduler(self.ctx)

        self.background_tasks: list[asyncio.Task[Any]] = []
        self.ssh_server: asyncssh.SSHAcceptor | None = None
        self.unix_server: asyncio.Server | None = None
        self.reverse_acceptor: asyncssh.SSHAcceptor | None = None
        self.http_server: web.AppRunner | None = None
        self.http_bound_port: int | None = None
        self.https_server: web.AppRunner | None = None
        self.https_bound_port: int | None = None
        self._started = False
        self._done_event = anyio.Event()

    @property
    def local_store(self) -> LocalStore:
        return self.ctx.local_store

    @property
    def stores(self) -> Mapping[StoreId, DaemonStore]:
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

    async def add_store(self, store: DaemonStore, dynamic: bool = False) -> None:
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
        if isinstance(local_store, LocalDBStore) and store.tracker.parent is not None:
            paths = await local_store.db.get_known_paths(store.store_id)
            if paths:
                store.tracker.add_known_paths(paths, update_regtime=False)
                log.info("loaded_cached_paths", store_id=store.store_id, count=len(paths))

        # Wire reconnect callback before start so the loop is ready
        captured_dynamic = dynamic

        async def _on_store_reconnect() -> None:
            if self.scheduler:
                self.scheduler.on_store_added(store, dynamic=captured_dynamic)

        store._on_reconnect = _on_store_reconnect

        try:
            await store.start()
        except Exception:
            log.warning(
                "store_start_failed",
                store_id=store.store_id,
                exc_info=True,
            )
            # Store is already in ctx._stores from construction.
            # Don't register with scheduler — it's unavailable.
            return

        # Server is the primary owner and mutator of the stores collection
        self.ctx._stores[store.store_id] = store

        if self.scheduler:
            self.scheduler.on_store_added(store, dynamic=dynamic)

    async def remove_store(self, store_id: StoreId, drain_timeout: float = 300.0) -> None:
        """Remove a remote store, cleaning DB records and closing connections.

        NOTE: Used by external projects — do not remove.
        """
        if store_id == StoreId("local"):
            raise RuntimeError("Cannot remove local store")
        if self.scheduler:
            # First, drain the store in the scheduler to cancel/requeue jobs
            await self.scheduler.drain_store(store_id, drain_timeout=drain_timeout)

        # Then remove from the context
        store = self.ctx._stores.pop(store_id, None)
        if store is None:
            log.warning("remove_store_not_found", store_id=store_id)
            return

        local_store = self.local_store
        if isinstance(local_store, LocalDBStore):
            try:
                async with local_store.db.acquire_conn() as conn:
                    await conn.execute(
                        "DELETE FROM PynixdKnownPaths WHERE storeId = ?",
                        (str(store_id),),
                    )
                    await conn.commit()
                    log.info("removed_store_path_data", store_id=store_id)
            except (aiosqlite.Error, RuntimeError):
                log.warning(
                    "remove_store_db_cleanup_failed",
                    store_id=store_id,
                    exc_info=True,
                )

        # Finally, close the store connection
        await store.close()

    async def _gc_tick(self) -> None:
        """Periodic GC loop. Runs at gc_interval."""
        log.info("gc_loop_started", interval=self.ctx.settings.gc_interval)
        while True:
            await anyio.sleep(self.ctx.settings.gc_interval)
            try:
                await PynixdCollectGarbageRequest.run_gc(
                    self.ctx,
                    PynixdGCAction.EXECUTE,
                    logs=OperationLogs(),
                )
            except anyio.get_cancelled_exc_class():
                return
            except Exception:
                log.exception("gc_pass_failed")

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

        if isinstance(local_store, LocalDBStore):
            self.ctx.db = local_store.db
            self.ctx.path_tracker.db = self.ctx.db
            log.info(
                "local_store_db_connected",
                db_path=str(self.ctx.db.db_path),
            )
        else:
            self.ctx.db = None
            self.ctx.path_tracker.db = None
            log.warning("local_store_db_disabled")

        local_store.tracker = self.ctx.path_tracker.create_instance(
            local_store.store_id,
            is_local=True,
        )

        if self.ctx.db:
            self.ctx.db.start()

        # Start non-local stores concurrently — they're already in _stores.
        async with anyio.create_task_group() as tg:
            for store_id, store in list(self.ctx._stores.items()):
                if store_id != StoreId("local"):
                    tg.start_soon(self.add_store, store)

        if self.ctx.scheduler:
            self.background_tasks.append(
                asyncio.create_task(self.ctx.scheduler.start()),
            )

        if self.ctx.db and self.ctx.settings.gc_enabled:
            self.background_tasks.append(asyncio.create_task(self._gc_tick()))

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

        self.reverse_acceptor = await start_reverse_acceptor(
            server=self,
            settings=s.reverse_acceptor,
        )

        if s.reverse_initiator.enabled:
            initiator = ReverseInitiator(self.ctx, s.reverse_initiator)
            self.background_tasks.append(asyncio.create_task(initiator.run()))

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

        if not (self.reverse_acceptor or self.ssh_server or self.unix_server or self.http_server or self.https_server):
            log.warning("no_servers_started")

    async def wait_finished(self) -> None:
        """Wait for the server to shut down.

        This returns when :meth:`close` has been called and all listeners
        and background tasks have been cleaned up.  Useful for library
        integrations that need to block until pynixd is fully stopped.
        """
        await self._done_event.wait()

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

        if self.reverse_acceptor:
            self.reverse_acceptor.close()
            await self.reverse_acceptor.wait_closed()
            self.reverse_acceptor = None
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

        if self.ctx.substitution_manager:
            await self.ctx.substitution_manager.close()

        for task in self.background_tasks:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self.background_tasks.clear()

        for store in self.ctx._stores.values():
            await store.close()
        self.ctx._stores.clear()
        self._done_event.set()
