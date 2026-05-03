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
from typing import TYPE_CHECKING

import structlog

from .operations.collect_garbage import CollectGarbageRequest, CollectGarbageResponse

from .types.aliases import StorePathSet
if TYPE_CHECKING:
    from collections.abc import Mapping

    from .context import PynixdContext
    from .local_store_db import LocalStoreDB
    from .store import Store
    from .store_path import StorePath
    from .types.ids import StoreId

log = structlog.get_logger(__name__)


class GarbageCollector:
    """Periodically deletes stale paths from all stores."""

    def __init__(self, ctx: PynixdContext) -> None:
        if ctx.db is None:
            raise ValueError("GarbageCollector requires a database")
        self.db: LocalStoreDB = ctx.db
        self.stores: Mapping[StoreId, Store] = ctx.stores
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

        if not self.stores and not local_stale:
            return

        log.info(
            "gc_pass_started",
            builder_stale_count=len(builder_stale),
            builder_max_age=self.builder_max_age,
            local_stale_count=len(local_stale) if local_stale else 0,
            local_max_age=self.local_max_age,
        )

        total_deleted = 0
        total_freed = 0
        store_tasks: list[tuple[Store, asyncio.Task[CollectGarbageResponse | None]]] = []

        try:
            async with asyncio.TaskGroup() as tg:
                # GC builders with builder lifetime
                for store in self.stores.values():
                    t = tg.create_task(self.gc_store(store, builder_stale))
                    store_tasks.append((store, t))

                # GC local store with local lifetime
                if local_stale:
                    t = tg.create_task(self.gc_store(self.local_store, local_stale))
                    store_tasks.append((self.local_store, t))
        except* Exception as eg:
            # TaskGroup cancels other tasks on first failure.
            # We use except* (Python 3.11+) to handle individual exceptions
            # in the ExceptionGroup if we wanted, but here we just want to
            # make sure we don't crash the GC loop.
            log.warning("gc_pass_interrupted_by_error", errors=eg.exceptions)

        for store, t in store_tasks:
            try:
                if not t.cancelled():
                    result = t.result()
                    if isinstance(result, CollectGarbageResponse):
                        total_deleted += len(result.paths_deleted)
                        total_freed += result.bytes_freed
            except Exception as e:
                # Individual task failure already logged/caught by TaskGroup?
                # Actually task.result() will re-raise if it failed.
                log.warning("gc_store_result_failed", store_id=store.store_id, error=str(e))

        log.info(
            "gc_pass_complete",
            paths_deleted=total_deleted,
            bytes_freed=total_freed,
        )

    async def gc_store(
        self,
        store: Store,
        paths: StorePathSet,
    ) -> CollectGarbageResponse | None:
        """Run CollectGarbage on a single store."""
        if not store.is_healthy:
            return None

        resp = await store.execute(
            CollectGarbageRequest(
                action=3,  # DeleteSpecific
                paths_to_delete=paths,
            ),
        )
        if resp.paths_deleted:
            log.info(
                "gc_store_complete",
                store_id=store.store_id,
                paths_deleted=len(resp.paths_deleted),
                bytes_freed=resp.bytes_freed,
            )
        return resp
