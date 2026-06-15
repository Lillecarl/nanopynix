"""Executor for QueryAllValidPaths (op 23) — SQLite fast-path, falls through to daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.query_all_valid_paths import (
    QUERY_ALL_VALID_PATHS,
    QueryAllValidPathsResponse,
)
from pynixd.store_path import StorePath

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryAllValidPathsExecutor(Executor):
    """Fast-path for QueryAllValidPaths — check SQLite, fall through to daemon."""

    op: ClassVar[int] = 23

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        if (db := store.db) is not None:
            try:
                async with db.execute(QUERY_ALL_VALID_PATHS) as cursor:
                    rows = await cursor.fetchall()
                return QueryAllValidPathsResponse(
                    paths={StorePath(r[0]) for r in rows},
                )
            except Exception:
                pass

        return None  # fall through to daemon
