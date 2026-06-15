"""Executor for QueryPathFromHashPart (op 29) — SQLite fast-path, falls through to daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.query_path_from_hash_part import (
    QUERY_PATH_FROM_HASH_PART,
    QueryPathFromHashPartResponse,
)
from pynixd.store_path import StorePath

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryPathFromHashPartExecutor(Executor):
    """Fast-path for QueryPathFromHashPart — check SQLite, fall through to daemon."""

    op: ClassVar[int] = 29

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        if (db := store.db) is not None:
            prefix = f"/nix/store/{request.path}"
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            async with db.execute(QUERY_PATH_FROM_HASH_PART, (prefix, upper)) as cursor:
                row = await cursor.fetchone()
            if row:
                result = QueryPathFromHashPartResponse(value=StorePath(row[0]))
                store.tracker.add_known_path(result.value)
                return result

        return None  # fall through to daemon
