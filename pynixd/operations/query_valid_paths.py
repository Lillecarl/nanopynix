"""QueryValidPaths operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse

QUERY_VALID_PATHS_BATCH = (
    "SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))"
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..store import Store


@dataclass
class QueryValidPathsResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)


@dataclass
class QueryValidPathsRequest(OpRequest[QueryValidPathsResponse]):
    op: ClassVar[int] = Op.QueryValidPaths
    response_type: ClassVar[type[OpResponse]] = QueryValidPathsResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)
    substitute: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        paths = await reader.read_string_set(StorePath)
        substitute = 0
        if version >= wire.proto(1, 27):
            substitute = await reader.read_uint64()
        return cls(paths=paths, substitute=substitute)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string_set(self.paths)
        if version >= wire.proto(1, 27):
            writer.write_uint64(self.substitute)

    async def execute_db(self, db: LocalStoreDB) -> QueryValidPathsResponse | None:
        paths_json = json.dumps(list(self.paths))
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_VALID_PATHS_BATCH, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
        return QueryValidPathsResponse(paths={StorePath(row[0]) for row in rows})

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryValidPathsResponse:
        try:
            result = await super().execute(store, client, suppress_last)
            if not self.substitute or result.paths >= self.paths:
                store.add_known_paths(result.paths)
                return result
        except Exception:
            pass

        resp = await super().execute(store, client, suppress_last)
        store.add_known_paths(resp.paths)
        return resp
