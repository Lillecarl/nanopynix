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
from typing import TYPE_CHECKING

import structlog

from .local_store_db import LocalStoreDB
from .operations.collect_garbage import CollectGarbageRequest, CollectGarbageResponse
from .store import Store
from .store_path import StorePath

if TYPE_CHECKING:
    from .context import PynixdContext

log = structlog.get_logger(__name__)


class GarbageCollector:
    """Periodically deletes stale paths from all stores."""

    def __init__(self, ctx: PynixdContext) -> None:
        if ctx.db is None:
            raise ValueError("GarbageCollector requires a database")
        self.db: LocalStoreDB = ctx.db
        self.stores: Mapping[str, Store] = ctx.stores
        self.local_store: Store = ctx.local_store
        self.interval = ctx.settings.gc_interval
        self.local_max_age = ctx.settings.gc_local_max_age
        self.builder_max_age = ctx.settings.gc_builder_max_age

    async def run(self) -> None:
        """Run GC passes at the configured interval."""
        log.info("gc_loop_started", interval=self.interval)
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
