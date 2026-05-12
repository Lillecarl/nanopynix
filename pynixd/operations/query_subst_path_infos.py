"""QuerySubstitutablePathInfos operation request/response types.

Deprecated in favor of QuerySubstitutablePaths (op 32).
Kept for backward compatibility with older daemon protocol versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..types.context import ReadContext, WriteContext
from .base import OpRequest, OpResponse, SubstitutablePathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class SubstitutablePathInfoEntry:
    path: str
    info: SubstitutablePathInfo


@dataclass
class QuerySubstitutablePathInfosResponse(OpResponse):
    entries: list[SubstitutablePathInfoEntry]

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        ctx = ReadContext(reader=reader, version=version, client=client, buffer_logs=buffer_logs)
        return await cls.deserialize(ctx)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        ctx = WriteContext(writer=writer, version=version)
        await self.serialize(ctx)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        n = await ctx.reader.read_uint64()
        obj.entries = []
        for _ in range(n):
            path = await ctx.reader.read_string()
            info = await SubstitutablePathInfo.deserialize(ctx)
            obj.entries.append(SubstitutablePathInfoEntry(path=path, info=info))
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", entry_count=len(self.entries))
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(len(self.entries))
        for entry in self.entries:
            ctx.writer.write_string(entry.path)
            entry.info.serialize(ctx)


@dataclass(kw_only=True)
class QuerySubstitutablePathInfosRequest(
    OpRequest[QuerySubstitutablePathInfosResponse],
):
    name: ClassVar[str] = "QuerySubstitutablePathInfos"
    op: ClassVar[int] = 30
    response_type: ClassVar[type[OpResponse]] = QuerySubstitutablePathInfosResponse
    is_query: ClassVar[bool] = True
    items: dict[str, str]

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
    ) -> Self:
        ctx = ReadContext(reader=reader, version=version)
        return await cls.deserialize(ctx)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        ctx = WriteContext(writer=writer, version=version)
        await self.serialize(ctx)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        n = await ctx.reader.read_uint64()
        obj.items = {}
        for _ in range(n):
            k = await ctx.reader.read_string()
            v = await ctx.reader.read_string()
            obj.items[k] = v
        obj.logger.debug("deserialize", item_count=n)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            ctx.writer.write_string(k)
            ctx.writer.write_string(v)
