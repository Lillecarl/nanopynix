"""QuerySubstitutablePathInfo operation request/response types.

Deprecated in favor of QuerySubstitutablePaths (op 32).
Kept for backward compatibility with older daemon protocol versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .base import OperationLogs, OpRequest, OpResponse, SubstitutablePathInfo

if TYPE_CHECKING:
    from ..types.context import ReadContext, WriteContext


@dataclass
class QuerySubstitutablePathInfoResponse(OpResponse):
    found: bool = False
    info: SubstitutablePathInfo | None = None

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.found = await ctx.reader.read_uint64() != 0
        obj.info = None
        if obj.found:
            obj.info = await SubstitutablePathInfo.deserialize(ctx)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", found=self.found)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(1 if self.found else 0)
        if self.found and self.info is not None:
            self.info.serialize(ctx)


@dataclass
class QuerySubstitutablePathInfoRequest(OpRequest[QuerySubstitutablePathInfoResponse]):
    name: ClassVar[str] = "QuerySubstitutablePathInfo"
    op: ClassVar[int] = 21
    response_type: ClassVar[type[OpResponse]] = QuerySubstitutablePathInfoResponse
    is_query: ClassVar[bool] = True
    path: str = ""

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.path = await ctx.reader.read_string()
        obj.logger.debug("deserialize", path=obj.path)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.path)
