"""QueryPathFromHashPart operation request/response types."""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OperationLogs, OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.context import ReadContext, WriteContext

QUERY_PATH_FROM_HASH_PART = """
SELECT path FROM ValidPaths WHERE path >= ? AND path < ? LIMIT 1
"""


@dataclass
class QueryPathFromHashPartResponse(OpResponse):
    value: StorePath = field(default_factory=lambda: StorePath(""))

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
                return QueryPathFromHashPartResponse(value=StorePath(row[0]))

        return await store.call(self, client=client, suppress_last=suppress_last)
