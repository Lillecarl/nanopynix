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
        logs = await OperationLogs.from_reader(reader)
        paths = await reader.read_string_set(StorePath)
        return cls(logs=logs, paths=paths)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", paths=self.paths)
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
        cls.logger.debug("from_reader")
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
            if (db := store.native_db) is not None:
                async with db.execute(QUERY_ALL_VALID_PATHS) as cursor:
                    rows = await cursor.fetchall()
                resp = QueryAllValidPathsResponse(paths={StorePath(r[0]) for r in rows})
                # Use set_known_paths to ensure the in-memory cache is fully synced with DB
                store.set_known_paths(resp.paths, update_regtime=False)
                self.logger.info(
                    "sync_paths_complete", store_id=store.id, count=len(resp.paths)
                )
                return resp

            # Remote store or no native DB: try the wire first.
            try:
                resp = await store.call(
                    self, client=client, suppress_last=suppress_last
                )
                # Success: Overwrite path tracker data (source of truth)
                store.set_known_paths(resp.paths, update_regtime=False)
                self.logger.info(
                    "sync_paths_complete", store_id=store.id, count=len(resp.paths)
                )
                return resp
            except Exception as e:
                if store.known_paths:
                    # If we already have paths (e.g. from DB on startup), verify them
                    # since the full sync failed.
                    self.logger.info(
                        "verifying_cached_paths",
                        store_id=store.id,
                        error=str(e),
                        count=len(store.known_paths),
                    )
                    from .query_valid_paths import QueryValidPathsRequest

                    verified = await store.execute(
                        QueryValidPathsRequest(paths=store.known_paths),
                        client=client,
                        suppress_last=suppress_last,
                    )
                    store.set_known_paths(verified.paths, update_regtime=False)
                    return QueryAllValidPathsResponse(paths=verified.paths)
                raise
        except Exception:
            self.logger.warning("sync_paths_failed", store_id=store.id)
            store.known_paths = set()
            return QueryAllValidPathsResponse(paths=set())
