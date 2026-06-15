"""Executor for QueryValidPaths (op 31) — SQLite fast-path, falls through to daemon."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.query_valid_paths import (
    QUERY_VALID_PATHS,
    QueryValidPathsResponse,
)
from pynixd.store_path import StorePath

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryValidPathsExecutor(Executor):
    """Fast-path for QueryValidPaths — check SQLite with JSON batch lookup, fall through to daemon."""

    op: ClassVar[int] = 31

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        if (db := store.db) is not None:
            paths_json = json.dumps([str(p) for p in request.paths])
            async with db.execute(QUERY_VALID_PATHS, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
            resp = QueryValidPathsResponse(paths={StorePath(r[0]) for r in rows})
            store.tracker.add_known_paths(resp.paths)
            return resp

        return None  # fall through to daemon
