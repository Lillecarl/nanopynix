"""OptimiseStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..types.context import ReadContext, WriteContext
from .base import OperationLogs, OpRequest, OpResponse, Role

if TYPE_CHECKING:
    from ..types import RequestContext


@dataclass
class OptimiseStoreResponse(OpResponse):
    value: int

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.value = await ctx.reader.read_uint64()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", value=self.value)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(self.value)


@dataclass
class OptimiseStoreRequest(OpRequest[OptimiseStoreResponse]):
    name: ClassVar[str] = "OptimiseStore"
    op: ClassVar[int] = 34
    response_type: ClassVar[type[OpResponse]] = OptimiseStoreResponse

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logger.debug("deserialize")
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)

    async def handle(self, ctx: RequestContext) -> OptimiseStoreResponse | None:
        self.logger.debug("received_op")

        # Must always consume the request to keep protocol in sync
        self = await self.deserialize(ReadContext.from_request(ctx))

        if ctx.role < Role.ADMIN:
            self.logger.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{self.name}' requires administrative privileges.",
            )
            return None

        result = await ctx.proxy.execute(self)
        self.logger.debug("responded_op")
        return result
