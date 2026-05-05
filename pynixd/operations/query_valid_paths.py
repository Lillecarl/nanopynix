"""QueryValidPaths operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..store_path import StorePath
from ..types import OperationLogs
from .base import OpRequest, OpResponse

QUERY_VALID_PATHS = """
SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
"""


if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.aliases import StorePathSet
    from ..wire import NixReader, NixWriter


@dataclass
class QueryValidPathsResponse(OpResponse):
    paths: StorePathSet

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(reader, client=client, buffer=buffer_logs)
        obj.paths = await reader.read_string_set(StorePath)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", paths=self.paths)
        self.logs.to_writer(writer)
        writer.write_string_set(self.paths)


@dataclass(kw_only=True)
class QueryValidPathsRequest(OpRequest[QueryValidPathsResponse]):
    name: ClassVar[str] = "QueryValidPaths"
    op: ClassVar[int] = 31
    response_type: ClassVar[type[OpResponse]] = QueryValidPathsResponse
    is_query: ClassVar[bool] = True
    paths: StorePathSet
    substitute: int

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.paths = await reader.read_string_set(StorePath)
        obj.substitute = 0
        if version >= wire.proto(1, 27):
            obj.substitute = await reader.read_uint64()
        obj.logger.debug("from_reader", paths=obj.paths, substitute=obj.substitute)
        return obj

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