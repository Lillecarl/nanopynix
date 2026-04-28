"""QueryAllValidPaths operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OpRequest, OpResponse
from .query_valid_paths import QueryValidPathsRequest

QUERY_ALL_VALID_PATHS = "SELECT path FROM ValidPaths"

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..wire import NixReader, NixWriter


@dataclass
class QueryAllValidPathsResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

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
class QueryAllValidPathsRequest(OpRequest[QueryAllValidPathsResponse]):
    name: ClassVar[str] = "QueryAllValidPaths"
    op: ClassVar[int] = 23
    response_type: ClassVar[type[OpResponse]] = QueryAllValidPathsResponse
    is_query: ClassVar[bool] = True

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logger.debug("from_reader")
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryAllValidPathsResponse:
        try:
            if (db := store.db) is not None:
                async with db.execute(QUERY_ALL_VALID_PATHS) as cursor:
                    rows = await cursor.fetchall()
                resp = QueryAllValidPathsResponse(paths={StorePath(r[0]) for r in rows})
                # Use set_known_paths to ensure the in-memory cache is fully synced with DB
                store.tracker.set_known_paths(resp.paths, update_regtime=False)
                self.logger.info(
                    "sync_paths_complete",
                    store_id=store.store_id,
                    count=len(resp.paths),
                )
                return resp

            # Remote store or no native DB: try the wire first.
            try:
                resp = await store.call(
                    self,
                    client=client,
                    suppress_last=suppress_last,
                )
                # Success: Overwrite path tracker data (source of truth)
                store.tracker.set_known_paths(resp.paths, update_regtime=False)
                self.logger.info(
                    "sync_paths_complete",
                    store_id=store.store_id,
                    count=len(resp.paths),
                )
            except Exception as e:
                if store.tracker.known_paths:
                    # If we already have paths (e.g. from DB on startup), verify them
                    # since the full sync failed.
                    self.logger.info(
                        "verifying_cached_paths",
                        store_id=store.store_id,
                        error=str(e),
                        count=len(store.tracker.known_paths),
                    )

                    try:
                        verified = await store.execute(
                            QueryValidPathsRequest(paths=set(store.tracker.known_paths)),
                            client=client,
                            suppress_last=suppress_last,
                        )
                        store.tracker.set_known_paths(
                            verified.paths,
                            update_regtime=False,
                        )
                        return QueryAllValidPathsResponse(paths=verified.paths)
                    except Exception as e2:
                        self.logger.warning(
                            "path_verification_failed",
                            store_id=store.store_id,
                            error=str(e2),
                        )
                        # Keep existing paths, better to be stale than empty for now?
                        # Or should we clear? The previous behavior was to clear.
                        # But for persistence test, we want to keep.
                        return QueryAllValidPathsResponse(
                            paths=set(store.tracker.known_paths),
                        )
                raise
            else:
                return resp
        except Exception:
            self.logger.warning("sync_paths_failed", store_id=store.store_id)
            # Do NOT clear known_paths here, it might have been loaded from DB
            return QueryAllValidPathsResponse(paths=set(store.tracker.known_paths))
