"""QueryAllValidPaths operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.aliases import StorePathSet
    from ..types.context import ReadContext, WriteContext

QUERY_ALL_VALID_PATHS = "SELECT path FROM ValidPaths"


@dataclass
class QueryAllValidPathsResponse(OpResponse):
    paths: StorePathSet

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


@dataclass
class QueryAllValidPathsRequest(OpRequest[QueryAllValidPathsResponse]):
    name: ClassVar[str] = "QueryAllValidPaths"
    op: ClassVar[int] = 23
    response_type: ClassVar[type[OpResponse]] = QueryAllValidPathsResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logger.debug("deserialize")
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryAllValidPathsResponse:
        if (db := store.db) is not None:
            try:
                async with db.execute(QUERY_ALL_VALID_PATHS) as cursor:
                    rows = await cursor.fetchall()
                return QueryAllValidPathsResponse(
                    paths={StorePath(r[0]) for r in rows},
                )
            except Exception:
                self.logger.warning(
                    "sync_paths_sqlite_error",
                    store_id=store.store_id,
                )

        return await store.call(
            self,
            client=client,
            suppress_last=suppress_last,
        )
