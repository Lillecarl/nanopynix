"""QueryClosure operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..protocol import Op
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
    from ..proxy import DaemonProxy
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class QueryClosureResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    @property
    def is_not_found(self) -> bool:
        return not self.paths

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)


@dataclass
class QueryClosureRequest(OpRequest[QueryClosureResponse]):
    op: ClassVar[int] = Op.QueryClosure
    response_type: ClassVar[type[OpResponse]] = QueryClosureResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string_set(self.paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryClosureResponse:
        if store.db is not None:
            seeds_json = json.dumps([str(p) for p in self.paths])
            async with store.db.acquire_conn() as conn:
                async with conn.execute(QUERY_CLOSURE, (seeds_json,)) as cursor:
                    rows = await cursor.fetchall()
            result = QueryClosureResponse(paths={StorePath(row[0]) for row in rows})
            store.add_known_paths(result.paths)
            return result

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        store.add_known_paths(resp.paths)
        return resp

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QueryClosureResponse:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        return await proxy.execute(request)
