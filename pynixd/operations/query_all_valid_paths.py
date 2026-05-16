"""QueryAllValidPaths operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..exceptions import BackendError, OpNotImplementedError
from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse
from .query_valid_paths import QueryValidPathsRequest

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.aliases import StorePathSet
    from ..types.context import ReadContext, WriteContext

QUERY_ALL_VALID_PATHS = "SELECT path FROM ValidPaths"


@dataclass
class QueryAllValidPathsResponse(OpResponse):
    paths: StorePathSet

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.paths = await ctx.reader.read_string_set(StorePath)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", paths=self.paths)
        self.logs.serialize(ctx)
        ctx.writer.write_string_set(self.paths)


@dataclass
class QueryAllValidPathsRequest(OpRequest[QueryAllValidPathsResponse]):
    name: ClassVar[str] = "QueryAllValidPaths"
    op: ClassVar[int] = 23
    response_type: ClassVar[type[OpResponse]] = QueryAllValidPathsResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logger.debug("deserialize")
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)

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
            except (BackendError, OSError, ConnectionError, EOFError, OpNotImplementedError) as e:
                # Try to get known paths from DB first, fallback to in-memory tracker
                known_paths: StorePathSet | None = None
                if store.tracker.parent is not None and store.tracker.parent.db is not None:
                    known_paths = await store.tracker.parent.db.get_known_paths(store.store_id)
                known_paths = known_paths or set(store.tracker.known_paths)

                if known_paths:
                    self.logger.info(
                        "verifying_cached_paths",
                        store_id=store.store_id,
                        error=str(e),
                        count=len(known_paths),
                    )

                    try:
                        verified = await store.execute(
                            QueryValidPathsRequest(
                                paths=known_paths,
                                substitute=0,
                            ),
                            client=client,
                            suppress_last=suppress_last,
                        )
                        # Remove stale paths — only keep the verified ones
                        stale = known_paths - verified.paths
                        if stale:
                            store.tracker.remove_known_paths(stale)
                        store.tracker.add_known_paths(verified.paths)
                        self.logger.info(
                            "sync_paths_verified",
                            store_id=store.store_id,
                            total=len(known_paths),
                            verified=len(verified.paths),
                            removed=len(stale),
                        )
                        return QueryAllValidPathsResponse(paths=verified.paths)
                    except (BackendError, OSError, ConnectionError, EOFError, OpNotImplementedError) as e2:
                        self.logger.warning(
                            "path_verification_failed",
                            store_id=store.store_id,
                            error=str(e2),
                        )
                        return QueryAllValidPathsResponse(paths=known_paths)
                raise
            else:
                return resp
        except Exception:
            self.logger.exception("sync_paths_failed", store_id=store.store_id)
            return QueryAllValidPathsResponse(paths=set())
