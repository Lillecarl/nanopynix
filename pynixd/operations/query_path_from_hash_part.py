"""QueryPathFromHashPart operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ..protocol import Op
from ..store_path import StorePath
from .base import OpResponse, SingleStringRequest, StorePathResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..store import Store

QUERY_PATH_FROM_HASH_PART = """
SELECT path FROM ValidPaths WHERE path >= ? AND path < ? LIMIT 1
"""


@dataclass
class QueryPathFromHashPartRequest(SingleStringRequest[StorePathResponse]):
    op: ClassVar[int] = Op.QueryPathFromHashPart
    response_type: ClassVar[type[OpResponse]] = StorePathResponse
    is_query: ClassVar[bool] = True

    async def execute_db(self, db: LocalStoreDB) -> StorePathResponse | None:
        prefix = f"/nix/store/{self.path}"
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        async with db.acquire_conn() as conn:
            async with conn.execute(
                QUERY_PATH_FROM_HASH_PART, (prefix, upper)
            ) as cursor:
                row = await cursor.fetchone()
        return StorePathResponse(value=StorePath(row[0])) if row else None

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StorePathResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None and result.value:
                store.add_known_path(StorePath(result.value))
                return result

        resp = await super().execute(store, client, suppress_last)
        if resp.value:
            store.add_known_path(StorePath(resp.value))
        return resp
