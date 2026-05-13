"""QueryClosure operation request/response types."""

from __future__ import annotations

import json
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


QUERY_CLOSURE = """
WITH RECURSIVE closure(id) AS (
    SELECT id FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
    UNION
    SELECT r.reference
    FROM closure c
    JOIN Refs r ON c.id = r.referrer
)
SELECT vp.path FROM closure c
JOIN ValidPaths vp ON c.id = vp.id
"""


@dataclass
class QueryClosureResponse(OpResponse):
    paths: StorePathSet

    @property
    def is_not_found(self) -> bool:
        return not self.paths

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
class QueryClosureRequest(OpRequest[QueryClosureResponse]):
    name: ClassVar[str] = "QueryClosure"
    op: ClassVar[int] = 104
    is_extension: ClassVar[bool] = True
    response_type: ClassVar[type[OpResponse]] = QueryClosureResponse
    is_query: ClassVar[bool] = True
    paths: StorePathSet

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.paths = await ctx.reader.read_string_set(StorePath)
        obj.logger.debug("deserialize", paths=obj.paths)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string_set(self.paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryClosureResponse:
        if (db := store.db) is not None:
            seeds_json = json.dumps([str(p) for p in self.paths])
            async with db.execute(QUERY_CLOSURE, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()
            result = QueryClosureResponse(paths={StorePath(row[0]) for row in rows})
            store.tracker.add_known_paths(result.paths)
            return result

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        store.tracker.add_known_paths(resp.paths)
        return resp
