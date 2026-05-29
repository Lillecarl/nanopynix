"""AddTempRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from ..types.auth import Role
from ..types.context import ReadContext, WriteContext
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..types import RequestContext as RequestContext


@dataclass
class AddTempRootResponse(OpResponse):
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
        self.logs.serialize(ctx)
        self.logger.debug("serialize", value=self.value)
        ctx.writer.write_uint64(self.value)


@dataclass(kw_only=True)
class AddTempRootRequest(OpRequest[AddTempRootResponse]):
    name: ClassVar[str] = "AddTempRoot"
    op: ClassVar[int] = 11
    response_type: ClassVar[type[OpResponse]] = AddTempRootResponse
    path: StorePath

    async def handle(self, ctx: RequestContext) -> AddTempRootResponse | None:
        if ctx.proxy.role == Role.ADMIN:
            self = await self.deserialize(ReadContext.from_request(ctx))
            return await ctx.proxy.execute(self)
        await ReadContext.from_request(ctx).reader.read_bytes()
        return AddTempRootResponse(value=1)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.path = await ctx.reader.read_string(StorePath)
        obj.logger.debug("deserialize", path=obj.path)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.path)
