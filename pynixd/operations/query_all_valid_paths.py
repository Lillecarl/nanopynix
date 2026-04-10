"""QueryAllValidPaths operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs

QUERY_ALL_VALID_PATHS = "SELECT path FROM ValidPaths"

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class QueryAllValidPathsResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            logs=await OperationLogs.from_reader(reader),
            paths=await reader.read_string_set(StorePath),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logs.to_writer(writer)
        writer.write_string_set(self.paths)


@dataclass
class QueryAllValidPathsRequest(OpRequest[QueryAllValidPathsResponse]):
    name: ClassVar[str] = "QueryAllValidPaths"
    op: ClassVar[int] = 23
    response_type: ClassVar[type[OpResponse]] = QueryAllValidPathsResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls()

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryAllValidPathsResponse:
        try:
            if store.db is not None:
                async with store.db.execute(QUERY_ALL_VALID_PATHS) as cursor:
                    rows = await cursor.fetchall()
                resp = QueryAllValidPathsResponse(paths={StorePath(r[0]) for r in rows})
                store.add_known_paths(resp.paths, update_regtime=False)
                self._log.info(
                    "sync_paths_complete", store_id=store.id, count=len(resp.paths)
                )
                return resp

            resp = await store.call(self, client=client, suppress_last=suppress_last)
            store.add_known_paths(resp.paths, update_regtime=False)
            self._log.info(
                "sync_paths_complete", store_id=store.id, count=len(resp.paths)
            )
            return resp
        except Exception:
            self._log.warning("sync_paths_failed", store_id=store.id)
            store.known_paths = set()
            return QueryAllValidPathsResponse(paths=set())
