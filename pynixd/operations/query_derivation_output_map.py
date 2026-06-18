"""QueryDerivationOutputMap operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.context import ReadContext, WriteContext


@dataclass
class QueryDerivationOutputMapResponse(OpResponse):
    items: dict[str, StorePath | None]

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        n = await ctx.reader.read_uint64()
        obj.items = {}
        for _ in range(n):
            k = await ctx.reader.read_string()
            raw = await ctx.reader.read_string()
            obj.items[k] = StorePath(raw) if raw else None
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", item_count=len(self.items))
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            ctx.writer.write_string(k)
            ctx.writer.write_string(v if v is not None else StorePath(""))


@dataclass(kw_only=True)
class QueryDerivationOutputMapRequest(OpRequest[QueryDerivationOutputMapResponse]):
    name: ClassVar[str] = "QueryDerivationOutputMap"
    op: ClassVar[int] = 41
    response_type: ClassVar[type[OpResponse]] = QueryDerivationOutputMapResponse
    is_query: ClassVar[bool] = True
    path: StorePath

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

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryDerivationOutputMapResponse:
        return await store.call(self, client=client, suppress_last=suppress_last)
