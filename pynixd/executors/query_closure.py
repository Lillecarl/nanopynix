"""Executor for QueryClosure (op 104) — SQLite recursive CTE fast-path, falls through to daemon."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.query_closure import (
    QUERY_CLOSURE,
    QueryClosureResponse,
)
from pynixd.store_path import StorePath

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryClosureExecutor(Executor):
    """Fast-path for QueryClosure — SQLite recursive CTE for closure computation, fall through to daemon."""

    op: ClassVar[int] = 104

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        if (db := store.db) is not None:
            seeds_json = json.dumps([str(p) for p in request.paths])
            async with db.execute(QUERY_CLOSURE, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()
            result = QueryClosureResponse(paths={StorePath(row[0]) for row in rows})
            store.tracker.add_known_paths(result.paths)
            return result

        return None  # fall through to daemon
