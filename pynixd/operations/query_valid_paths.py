"""QueryValidPaths operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.aliases import StorePathSet
    from ..types.context import ReadContext, WriteContext

QUERY_VALID_PATHS = """
SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
"""


@dataclass
class QueryValidPathsResponse(OpResponse):
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
class QueryValidPathsRequest(OpRequest[QueryValidPathsResponse]):
    name: ClassVar[str] = "QueryValidPaths"
    op: ClassVar[int] = 31
    response_type: ClassVar[type[OpResponse]] = QueryValidPathsResponse
    is_query: ClassVar[bool] = True
    paths: StorePathSet
    substitute: int

    # ── New-style API (ReadContext / WriteContext) ──────────────

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.paths = await ctx.reader.read_string_set(StorePath)
        obj.substitute = 0
        if ctx.version >= wire.proto(1, 27):
            obj.substitute = await ctx.reader.read_uint64()
        obj.logger.debug("deserialize", paths=obj.paths, substitute=obj.substitute)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string_set(self.paths)
        if ctx.version >= wire.proto(1, 27):
            ctx.writer.write_uint64(self.substitute)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryValidPathsResponse:
        if (db := store.db) is not None:
            paths_json = json.dumps([str(p) for p in self.paths])
            async with db.execute(
                QUERY_VALID_PATHS,
                (paths_json,),
            ) as cursor:
                rows = await cursor.fetchall()
            resp = QueryValidPathsResponse(paths={StorePath(r[0]) for r in rows})
            store.tracker.add_known_paths(resp.paths)
            return resp

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        store.tracker.add_known_paths(resp.paths)
        return resp
