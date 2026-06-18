"""BuildDerivation operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from ..types.context import ReadContext, WriteContext
from .base import BasicDerivation, BuildMode, BuildResult, OpRequest, OpResponse

if TYPE_CHECKING:
    from ..types import RequestContext


@dataclass
class BuildDerivationResponse(OpResponse):
    result: BuildResult

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.result = await BuildResult.deserialize(ctx)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logs.serialize(ctx)
        await self.result.serialize(ctx)


@dataclass(kw_only=True)
class BuildDerivationRequest(OpRequest[BuildDerivationResponse]):
    name: ClassVar[str] = "BuildDerivation"
    op: ClassVar[int] = 36
    response_type: ClassVar[type[OpResponse]] = BuildDerivationResponse
    is_build: ClassVar[bool] = True
    drv_path: StorePath
    derivation: BasicDerivation
    build_mode: BuildMode

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.drv_path = await ctx.reader.read_string(StorePath)
        obj.derivation = await BasicDerivation.deserialize(ctx)
        obj.build_mode = BuildMode(await ctx.reader.read_uint64())
        obj.logger.debug(
            "deserialize",
            drv_path=obj.drv_path,
            build_mode=obj.build_mode,
        )
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.drv_path)
        await self.derivation.serialize(ctx)
        ctx.writer.write_uint64(self.build_mode)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")

        self = await self.deserialize(ReadContext.from_request(ctx))

        if not ctx.proxy.use_scheduler_for_builds:
            self.logger.debug("handle_local_mode_fallback")
            result = await ctx.proxy.local_store.execute(self, client=ctx.proxy.client)

            self.logger.debug("responded_op")
            return result

        # The client provides a complete build recipe in BuildDerivation.
        # input_srcs contains all required dependencies (sources and other .drvs).
        # We don't need to perform extra discovery or closure expansion.
        if ctx.proxy.scheduler is None:
            raise RuntimeError("BuildDerivation requires a configured scheduler")

        build_id, future = await ctx.proxy.scheduler.build_derivation(
            self,
        )
        if ctx.proxy.client is not None:
            await ctx.proxy.scheduler.queue.subscribe(build_id, ctx.proxy.client)
        self.logger.info(
            "build_derivation_enqueued",
            build_id=build_id,
            drv_path=self.drv_path,
            required_count=len(self.derivation.input_srcs),
        )
        response = await future
        self.logger.debug("responded_op")
        return response
