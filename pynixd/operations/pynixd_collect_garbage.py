"""Custom pynixd operation: trigger GC on the daemon.

DRY_RUN is a no-op. EXECUTE triggers a single GC pass (same as the
interval timer). Not forwarded to remote stores — pynixd-server-local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..types import PynixdGCAction
from ..types import Role as Role
from ..types.context import ReadContext
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..gc import GarbageCollector
    from ..types import RequestContext as RequestContext
    from ..types.context import WriteContext


@dataclass
class PynixdCollectGarbageResponse(OpResponse):
    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logs = await OperationLogs.deserialize(ctx)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logs.serialize(ctx)


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

        gc: GarbageCollector | None = ctx.proxy.ctx.gc
        if gc is None:
            self.logger.warning("gc_not_available")
            await ctx.proxy.send_error(
                "Garbage collector is not available (no database configured).",
            )
            return None

        match self.action:
            case PynixdGCAction.DRY_RUN:
                self.logger.info("gc_dry_run", message="Would trigger GC pass (dry-run)")

            case PynixdGCAction.EXECUTE:
                self.logger.info("gc_execute_start", message="Triggering GC pass")
                await gc.run_gc_pass()
                self.logger.info("gc_execute_complete", message="GC pass complete")

        return PynixdCollectGarbageResponse()
