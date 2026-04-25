"""QueryValidPaths operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OperationLogs, OpRequest, OpResponse

QUERY_VALID_PATHS = """
SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class QueryValidPathsResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        self.paths = await reader.read_string_set(StorePath)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", paths=self.paths)
        self.logs.to_writer(writer)
        writer.write_string_set(self.paths)


@dataclass
class QueryValidPathsRequest(OpRequest[QueryValidPathsResponse]):
    name: ClassVar[str] = "QueryValidPaths"
    op: ClassVar[int] = 31
    response_type: ClassVar[type[OpResponse]] = QueryValidPathsResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)
    substitute: int = 0

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.paths = await reader.read_string_set(StorePath)
        self.substitute = 0
        if version >= wire.proto(1, 27):
            self.substitute = await reader.read_uint64()
        self.logger.debug("from_reader", paths=self.paths, substitute=self.substitute)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.paths)
        if version >= wire.proto(1, 27):
            writer.write_uint64(self.substitute)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryValidPathsResponse:
        if (db := store.native_db) is not None:
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
