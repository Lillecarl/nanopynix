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
from .operations.maintenance import CollectGarbageResponse
from .store import Store

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
        self._db = db
        self._stores = stores
        self._local_store = local_store
        self._interval = interval
        self._local_max_age = local_max_age
        self._builder_max_age = builder_max_age
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the GC loop. Call from async context."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the GC loop."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        """Run GC passes at the configured interval."""
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._run_gc_pass()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("GC pass failed")

    async def _run_gc_pass(self) -> None:
        """Find stale paths and delete them from all stores."""
        # Query with the shorter lifetime to get the superset of stale paths
        builder_stale = await self._db.query_stale_paths(self._builder_max_age)
        if not builder_stale:
            return

        # Local store uses longer lifetime — filter to older paths
        local_stale = await self._db.query_stale_paths(self._local_max_age)

        tasks = []
        stores_for_tasks: list[Store] = []

        # GC builders with builder lifetime
        for store in self._stores.values():
            tasks.append(self._gc_store(store, builder_stale))
            stores_for_tasks.append(store)

        # GC local store with local lifetime
        if local_stale:
            tasks.append(self._gc_store(self._local_store, local_stale))
            stores_for_tasks.append(self._local_store)

        if not tasks:
            return

        log.info(
            "GC pass: %d builder-stale paths (>%ds), %d local-stale paths (>%ds)",
            len(builder_stale),
            self._builder_max_age,
            len(local_stale) if local_stale else 0,
            self._local_max_age,
        )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_deleted = 0
        total_freed = 0
        for store, result in zip(stores_for_tasks, results):
            if isinstance(result, BaseException):
                log.warning("GC failed", store_id=store.id, error=result)
            elif isinstance(result, CollectGarbageResponse):
                total_deleted += len(result.paths_deleted)
                total_freed += result.bytes_freed

        log.info(
            "GC pass done: %d paths deleted, %d bytes freed",
            total_deleted,
            total_freed,
        )

    async def _gc_store(
        self,
        store: Store,
        paths: set[str],
    ) -> CollectGarbageResponse | None:
        """Run CollectGarbage on a single store."""
        if not store.is_healthy:
            return None

        resp = await store.collect_garbage(paths)
        if resp.paths_deleted:
            log.info(
                "GC %s: deleted %d paths, freed %d bytes",
                store.id,
                len(resp.paths_deleted),
                resp.bytes_freed,
            )
        return resp
