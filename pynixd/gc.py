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

from .exceptions import BackendError
from .operations.collect_garbage import (
    CollectGarbageRequest,
    CollectGarbageResponse,
    GCAction,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .context import PynixdContext
    from .local_store_db import LocalStoreDB
    from .store import Store
    from .types.aliases import StorePathSet
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
        self.gc_enabled = ctx.settings.gc_enabled
        self.interval = ctx.settings.gc_interval
        self.local_max_age = ctx.settings.gc_local_max_age
        self.builder_max_age = ctx.settings.gc_builder_max_age

    async def run(self) -> None:
        """Run GC passes at the configured interval, if globally enabled."""
        if not self.gc_enabled:
            log.info("gc_disabled_globally")
            return
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
        """Find stale paths and delete them from all stores.

        Respects per-store ``gc_enabled`` and ``gc_max_age``.
        A store with ``gc_enabled=False`` is skipped entirely.
        A store with ``gc_max_age`` set uses that value instead of the
        global builder/local default.
        """
        stale_cache: dict[int, StorePathSet] = {}

        async def get_stale(age: int) -> StorePathSet:
            if age not in stale_cache:
                result = await self.db.query_stale_paths(age)
                stale_cache[age] = result or set()
            return stale_cache[age]

        store_gc_targets: list[tuple[Store, StorePathSet]] = []

        for store in self.stores.values():
            if not store.gc_enabled:
                continue
            effective_age = store.gc_max_age or self.builder_max_age
            paths = await get_stale(effective_age)
            if paths:
                store_gc_targets.append((store, paths))

        if self.local_store.gc_enabled:
            effective_age = self.local_store.gc_max_age or self.local_max_age
            local_stale = await get_stale(effective_age)
            if local_stale:
                store_gc_targets.append((self.local_store, local_stale))

        if not store_gc_targets:
            return

        log.info(
            "gc_pass_started",
            stores=len(store_gc_targets),
            ages_queried=list(stale_cache.keys()),
        )

        total_deleted = 0
        total_freed = 0
        store_tasks: list[tuple[Store, asyncio.Task[CollectGarbageResponse | None]]] = []

        try:
            async with asyncio.TaskGroup() as tg:
                for store, paths in store_gc_targets:
                    t = tg.create_task(self.gc_store(store, paths))
                    store_tasks.append((store, t))
        except* Exception as eg:
            log.warning("gc_pass_interrupted_by_error", errors=eg.exceptions)

        for store, t in store_tasks:
            try:
                if not t.cancelled():
                    result = t.result()
                    if isinstance(result, CollectGarbageResponse):
                        total_deleted += len(result.paths_deleted)
                        total_freed += result.bytes_freed
            except (BackendError, OSError, ConnectionError) as e:
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
        """Run CollectGarbage on a single store.

        Two-phase: first RETURN_DEAD to discover paths that are not
        GC-rooted, then DELETE_SPECIFIC only on the intersection of
        stale paths and actually-dead paths.
        """
        if not store.is_healthy:
            return None

        # Phase 1: discover dead (non-rooted) paths
        try:
            dead_resp = await store.execute(
                CollectGarbageRequest(
                    action=GCAction.RETURN_DEAD,
                    paths_to_delete=set(),
                    ignore_liveness=0,
                    max_freed=0,
                    _obsolete1=0,
                    _obsolete2=0,
                    _obsolete3=0,
                ),
            )
        except BackendError:
            log.warning("gc_return_dead_failed", store_id=store.store_id)
            return None

        dead_paths = dead_resp.paths_deleted
        if not dead_paths:
            return None

        # Phase 2: only delete paths that are both stale and dead
        to_delete = paths & dead_paths
        if not to_delete:
            return None

        resp = await store.execute(
            CollectGarbageRequest(
                action=GCAction.DELETE_SPECIFIC,
                paths_to_delete=to_delete,
                ignore_liveness=0,
                max_freed=0,
                _obsolete1=0,
                _obsolete2=0,
                _obsolete3=0,
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
