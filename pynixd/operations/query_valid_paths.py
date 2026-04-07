"""QueryValidPaths operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, StringSetResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..store import Store

QUERY_VALID_PATHS_BATCH = (
    "SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))"
)


@dataclass
class QueryValidPathsRequest(OpRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryValidPaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
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
        writer.write_string_set(self.paths)
        if version >= wire.proto(1, 27):
            writer.write_uint64(self.substitute)

    async def execute_db(self, db: LocalStoreDB) -> StringSetResponse | None:
        paths_json = json.dumps(list(self.paths))
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_VALID_PATHS_BATCH, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
        return StringSetResponse(paths={StorePath(row[0]) for row in rows})

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StringSetResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None:
                if not self.substitute or result.paths >= self.paths:
                    store.add_known_paths(result.paths)
                    return result

        resp = await super().execute(store, client, suppress_last)
        store.add_known_paths(resp.paths)
        return resp
