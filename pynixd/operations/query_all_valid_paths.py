"""QueryAllValidPaths operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ..protocol import Op
from ..store_path import StorePath
from .base import EmptyRequest, OpResponse, StringSetResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..store import Store

QUERY_ALL_VALID_PATHS = "SELECT path FROM ValidPaths"


@dataclass
class QueryAllValidPathsRequest(EmptyRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryAllValidPaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True

    async def execute_db(self, db: LocalStoreDB) -> StringSetResponse | None:
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_ALL_VALID_PATHS) as cursor:
                rows = await cursor.fetchall()
        return StringSetResponse(paths={StorePath(r[0]) for r in rows})

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StringSetResponse:
        try:
            resp = await super().execute(store, client, suppress_last)
            store.add_known_paths(resp.paths, update_regtime=False)
            self._log.info(
                "sync_paths_complete", store_id=store.id, count=len(resp.paths)
            )
            return resp
        except Exception:
            self._log.warning("sync_paths_failed", store_id=store.id)
            store.known_paths = set()
            return StringSetResponse(paths=set())
