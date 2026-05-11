"""QueryPathFromHashPart operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OperationLogs, OpRequest, OpResponse

QUERY_PATH_FROM_HASH_PART = """
SELECT path FROM ValidPaths WHERE path >= ? AND path < ? LIMIT 1
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.context import ReadContext, WriteContext
    from ..wire import NixReader, NixWriter


@dataclass
class QueryPathFromHashPartResponse(OpResponse):
    value: StorePath = field(default_factory=lambda: StorePath(""))

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        obj.value = await reader.read_string(StorePath)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_string(self.value)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.value = await ctx.reader.read_string(StorePath)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", value=self.value)
        self.logs.serialize(ctx)
        ctx.writer.write_string(self.value)


@dataclass
class QueryPathFromHashPartRequest(OpRequest[QueryPathFromHashPartResponse]):
    name: ClassVar[str] = "QueryPathFromHashPart"
    op: ClassVar[int] = 29
    response_type: ClassVar[type[OpResponse]] = QueryPathFromHashPartResponse
    is_query: ClassVar[bool] = True
    path: str = ""

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,  # noqa: ARG003
        buffer_logs: bool = True,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.path = await reader.read_string()
        obj.logger.debug("from_reader", path=obj.path)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)

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

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathFromHashPartResponse:
        if (db := store.db) is not None:
            prefix = f"/nix/store/{self.path}"
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            async with db.execute(QUERY_PATH_FROM_HASH_PART, (prefix, upper)) as cursor:
                row = await cursor.fetchone()
            if row:
                result = QueryPathFromHashPartResponse(value=StorePath(row[0]))
                store.tracker.add_known_path(result.value)
                return result

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        if resp.value:
            store.tracker.add_known_path(StorePath(resp.value))
        return resp
