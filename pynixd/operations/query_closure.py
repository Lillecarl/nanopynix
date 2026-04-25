"""QueryClosure operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse

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

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class QueryClosureResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    @property
    def is_not_found(self) -> bool:
        return not self.paths

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(reader, client=client, buffer=buffer_logs)
        self.paths = await reader.read_string_set(StorePath)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", paths=self.paths)
        self.logs.to_writer(writer)
        writer.write_string_set(self.paths)


@dataclass
class QueryClosureRequest(OpRequest[QueryClosureResponse]):
    name: ClassVar[str] = "QueryClosure"
    op: ClassVar[int] = 104
    is_extension: ClassVar[bool] = True
    response_type: ClassVar[type[OpResponse]] = QueryClosureResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.paths = await reader.read_string_set(StorePath)
        self.logger.debug("from_reader", paths=self.paths)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryClosureResponse:
        if (db := store.native_db) is not None:
            seeds_json = json.dumps([str(p) for p in self.paths])
            async with db.execute(QUERY_CLOSURE, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()
            result = QueryClosureResponse(paths={StorePath(row[0]) for row in rows})
            store.tracker.add_known_paths(result.paths)
            return result

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        store.tracker.add_known_paths(resp.paths)
        return resp
