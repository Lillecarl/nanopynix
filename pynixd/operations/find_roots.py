"""FindRoots operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..types.context import ReadContext, WriteContext


@dataclass
class FindRootsEntry:
    link: str
    target: str


@dataclass
class FindRootsResponse(OpResponse):
    roots: list[FindRootsEntry]

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        n = await ctx.reader.read_uint64()
        obj.roots = []
        for _ in range(n):
            link = await ctx.reader.read_string()
            target = await ctx.reader.read_string()
            obj.roots.append(FindRootsEntry(link=link, target=target))
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", root_count=len(self.roots))
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(len(self.roots))
        for root in self.roots:
            ctx.writer.write_string(root.link)
            ctx.writer.write_string(root.target)


@dataclass
class FindRootsRequest(OpRequest[FindRootsResponse]):
    name: ClassVar[str] = "FindRoots"
    op: ClassVar[int] = 14
    response_type: ClassVar[type[OpResponse]] = FindRootsResponse

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logger.debug("deserialize")
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
