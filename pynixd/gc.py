"""Periodic garbage collection of stale store paths.

Queries the local store DB for paths with old registrationTime and
issues CollectGarbage DeleteSpecific to all backends and the local store.

Separate lifetimes for local and builder stores:
- PYNIXD_GC_LOCAL_MAX_AGE: local store path lifetime (default: 604800 = 1 week)
- PYNIXD_GC_BUILDER_MAX_AGE: builder store path lifetime (default: 3600 = 1 hour)
- PYNIXD_GC_INTERVAL: how often to run GC passes (default: 3600 = 1 hour)
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import structlog
from environs import Env

from .local_store_db import LocalStoreDB
from .operations.maintenance import CollectGarbageRequest, CollectGarbageResponse
from .store import Store
from .store_path import StorePath

log = structlog.get_logger(__name__)

env = Env()

_GC_INTERVAL = env.float("PYNIXD_GC_INTERVAL", 3600.0)
_GC_LOCAL_MAX_AGE = env.int("PYNIXD_GC_LOCAL_MAX_AGE", 604800)
_GC_BUILDER_MAX_AGE = env.int("PYNIXD_GC_BUILDER_MAX_AGE", 3600)


class GarbageCollector:
    """Periodically deletes stale paths from all stores."""

    def __init__(
        self,
        db: LocalStoreDB,
        stores: Mapping[str, Store],
        local_store: Store,
        interval: float = _GC_INTERVAL,
        local_max_age: int = _GC_LOCAL_MAX_AGE,
        builder_max_age: int = _GC_BUILDER_MAX_AGE,
    ) -> None:
        self.db = db
        self.stores = stores
        self.local_store = local_store
        self.interval = interval
        self.local_max_age = local_max_age
        self.builder_max_age = builder_max_age
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the GC loop. Call from async context."""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.gc_loop())

    async def stop(self) -> None:
        """Stop the GC loop."""
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def gc_loop(self) -> None:
        """Run GC passes at the configured interval."""
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.run_gc_pass()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("gc_pass_failed")

    async def run_gc_pass(self) -> None:
        """Find stale paths and delete them from all stores."""
        # Query with the shorter lifetime to get the superset of stale paths
        builder_stale = await self.db.query_stale_paths(self.builder_max_age)
        if not builder_stale:
            return

        # Local store uses longer lifetime — filter to older paths
        local_stale = await self.db.query_stale_paths(self.local_max_age)

        tasks = []
        stores_for_tasks: list[Store] = []

        # GC builders with builder lifetime
        for store in self.stores.values():
            tasks.append(self.gc_store(store, builder_stale))
            stores_for_tasks.append(store)

        # GC local store with local lifetime
        if local_stale:
            tasks.append(self.gc_store(self.local_store, local_stale))
            stores_for_tasks.append(self.local_store)

        if not tasks:
            return

        log.info(
            "gc_pass_started",
            builder_stale_count=len(builder_stale),
            builder_max_age=self.builder_max_age,
            local_stale_count=len(local_stale) if local_stale else 0,
            local_max_age=self.local_max_age,
        )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_deleted = 0
        total_freed = 0
        for store, result in zip(stores_for_tasks, results):
            if isinstance(result, BaseException):
                log.warning("gc_store_failed", store_id=store.id, error=result)
            elif isinstance(result, CollectGarbageResponse):
                total_deleted += len(result.paths_deleted)
                total_freed += result.bytes_freed

        log.info(
            "gc_pass_complete",
            paths_deleted=total_deleted,
            bytes_freed=total_freed,
        )

    async def gc_store(
        self,
        store: Store,
        paths: set[StorePath],
    ) -> CollectGarbageResponse | None:
        """Run CollectGarbage on a single store."""
        if not store.is_healthy:
            return None

        resp = await store.execute(
            CollectGarbageRequest(
                action=3,  # DeleteSpecific
                paths_to_delete=paths,
            )
        )
        if resp.paths_deleted:
            log.info(
                "gc_store_complete",
                store_id=store.id,
                paths_deleted=len(resp.paths_deleted),
                bytes_freed=resp.bytes_freed,
            )
        return resp
