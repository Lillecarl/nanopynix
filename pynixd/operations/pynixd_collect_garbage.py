"""Custom pynixd operation: trigger GC on the daemon.

DRY_RUN is a no-op. EXECUTE triggers a single GC pass (same as the
interval timer). Not forwarded to remote stores — pynixd-server-local.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..exceptions import BackendError
from ..operations.collect_garbage import (
    CollectGarbageRequest,
    CollectGarbageResponse,
    GCAction,
)
from ..stderr import OperationLogs, StderrNext
from ..store_path import StorePath
from ..types import PynixdGCAction
from ..types import Role as Role
from ..types.context import ReadContext
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..context import PynixdContext
    from ..store.base import Store as Store
    from ..store.daemon import DaemonStore as DaemonStore
    from ..types import RequestContext as RequestContext
    from ..types.aliases import StorePathSet
    from ..types.context import WriteContext

log = structlog.get_logger(__name__)


@dataclass
class PynixdCollectGarbageResponse(OpResponse):
    store_paths: set[StorePath] = field(default_factory=set)
    bytes: int = 0

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.store_paths = await ctx.reader.read_string_set(StorePath)
        obj.bytes = await ctx.reader.read_uint64()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logs.serialize(ctx)
        ctx.writer.write_string_set(self.store_paths)
        ctx.writer.write_uint64(self.bytes)


@dataclass(kw_only=True)
class PynixdCollectGarbageRequest(OpRequest[PynixdCollectGarbageResponse]):
    name: ClassVar[str] = "PynixdCollectGarbage"
    op: ClassVar[int] = 101
    response_type: ClassVar[type[OpResponse]] = PynixdCollectGarbageResponse
    is_extension: ClassVar[bool] = True

    action: PynixdGCAction = field(default_factory=lambda: PynixdGCAction.DRY_RUN)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.action = PynixdGCAction(await ctx.reader.read_uint64())
        obj.logger.debug("deserialize", action=obj.action)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_uint64(self.action)
        await ctx.writer.drain()

    async def handle(self, ctx: RequestContext) -> PynixdCollectGarbageResponse | None:
        self.logger.debug("received_op")

        self = await self.deserialize(ReadContext.from_request(ctx))

        if ctx.role < Role.ADMIN:
            self.logger.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{self.name}' requires administrative privileges.",
            )
            return None

        response = PynixdCollectGarbageResponse()
        response.store_paths, response.bytes = await self.run_gc(
            ctx.proxy.ctx,
            self.action,
            logs=response.logs,
        )
        return response

    @classmethod
    async def run_gc(
        cls,
        pynixd_ctx: PynixdContext,
        action: PynixdGCAction,
        *,
        logs: OperationLogs,
    ) -> tuple[set[StorePath], int]:
        """Execute or dry-run a GC pass across all stores.

        Two-phase GC per store:
        1. RETURN_DEAD to discover paths not GC-rooted.
        2. DELETE_SPECIFIC on the intersection of stale and dead paths.

        Failures are appended to ``logs`` as ``StderrNext`` messages so they
        are forwarded to the client or captured by the ticker.

        Returns ``(store_paths, bytes)`` — the deleted (or dry-run eligible)
        paths and total freed bytes. Both are zero/empty for early exits.
        """
        settings = pynixd_ctx.settings
        if not settings.gc_enabled:
            log.info("gc_disabled_globally")
            return set(), 0

        db = pynixd_ctx.db
        if db is None:
            log.info("gc_no_database")
            return set(), 0

        stale_cache: dict[int, StorePathSet] = {}

        async def get_stale(age: int) -> StorePathSet:
            if age not in stale_cache:
                result = await db.query_stale_paths(age)
                stale_cache[age] = result or set()
            return stale_cache[age]

        store_gc_targets: list[tuple[DaemonStore, StorePathSet]] = []

        for store in pynixd_ctx.stores.values():
            if not store.gc_enabled:
                continue
            if store.gc_max_age is None:
                continue
            paths = await get_stale(store.gc_max_age)
            if paths:
                store_gc_targets.append((store, paths))

        if not store_gc_targets:
            return set(), 0

        dry_run = action == PynixdGCAction.DRY_RUN

        logs.add(
            StderrNext(f"pynixd: GC pass started on {len(store_gc_targets)} stores{' (dry-run)' if dry_run else ''}"),
        )

        all_paths: set[StorePath] = set()
        total_bytes = 0
        store_tasks: list[tuple[DaemonStore, asyncio.Task[CollectGarbageResponse | None]]] = []

        try:
            async with asyncio.TaskGroup() as tg:
                for store, paths in store_gc_targets:
                    t = tg.create_task(cls._gc_store(store, paths, logs=logs, dry_run=dry_run))
                    store_tasks.append((store, t))
        except* Exception as eg:
            log.warning("gc_pass_interrupted_by_error", errors=eg.exceptions)

        for store, t in store_tasks:
            try:
                if not t.cancelled():
                    result = t.result()
                    if isinstance(result, CollectGarbageResponse):
                        all_paths |= result.paths_deleted
                        total_bytes += result.bytes_freed
            except (BackendError, OSError, ConnectionError) as e:
                logs.add(StderrNext(f"pynixd: gc_store failed for {store.store_id}: {e}"))
                log.warning("gc_store_result_failed", store_id=store.store_id, error=str(e))

        logs.add(
            StderrNext(
                f"pynixd: GC pass {'(dry-run) ' if dry_run else ''}complete: "
                f"{len(all_paths)} paths, {total_bytes} bytes freed",
            ),
        )
        return all_paths, total_bytes

    @classmethod
    async def _gc_store(
        cls,
        store: DaemonStore,
        paths: StorePathSet,
        *,
        logs: OperationLogs,
        dry_run: bool = False,
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
        except BackendError as e:
            logs.add(StderrNext(f"pynixd: gc RETURN_DEAD failed for {store.store_id}: {e}"))
            log.warning("gc_return_dead_failed", store_id=store.store_id, error=str(e))
            return None

        dead_paths = dead_resp.paths_deleted
        if not dead_paths:
            return None

        # Phase 2: only delete paths that are both stale and dead
        to_delete = paths & dead_paths
        if not to_delete:
            return None

        if dry_run:
            return CollectGarbageResponse(
                paths_deleted=to_delete,
                bytes_freed=0,
                _obsolete=0,
            )

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
