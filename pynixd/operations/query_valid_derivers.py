"""QueryValidDerivers operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..types.aliases import StorePathSet
    from ..types.context import ReadContext, WriteContext


@dataclass
class QueryValidDeriversResponse(OpResponse):
    paths: StorePathSet

    # ── New-style API (ReadContext / WriteContext) ──────────────

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.paths = await ctx.reader.read_string_set(StorePath)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", paths=self.paths)
        self.logs.serialize(ctx)
        ctx.writer.write_string_set(self.paths)


@dataclass(kw_only=True)
class QueryValidDeriversRequest(OpRequest[QueryValidDeriversResponse]):
    name: ClassVar[str] = "QueryValidDerivers"
    op: ClassVar[int] = 33
    response_type: ClassVar[type[OpResponse]] = QueryValidDeriversResponse
    is_query: ClassVar[bool] = True
    path: StorePath

    # ── New-style API (ReadContext / WriteContext) ──────────────

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
