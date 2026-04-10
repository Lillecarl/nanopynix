"""QueryValidPaths operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs

QUERY_VALID_PATHS_BATCH = (
    "SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))"
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class QueryValidPathsResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            logs=await OperationLogs.from_reader(reader),
            paths=await reader.read_string_set(StorePath),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)
        self.logs.to_writer(writer)


@dataclass
class QueryValidPathsRequest(OpRequest[QueryValidPathsResponse]):
    name: ClassVar[str] = "QueryValidPaths"
    op: ClassVar[int] = 31
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

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryValidPathsResponse:
        if store.db is not None:
            paths_json = json.dumps(list(self.paths))
            async with store.db.execute(
                QUERY_VALID_PATHS_BATCH, (paths_json,)
            ) as cursor:
                rows = await cursor.fetchall()
            result = QueryValidPathsResponse(paths={StorePath(row[0]) for row in rows})
            store.add_known_paths(result.paths)
            return result

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        store.add_known_paths(resp.paths)
        return resp
