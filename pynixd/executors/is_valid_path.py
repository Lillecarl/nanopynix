"""Executor for IsValidPath (op 1) — tracker cache + SQLite fast-path."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.is_valid_path import (
    IS_VALID_PATH,
    IsValidPathResponse,
)

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class IsValidPathExecutor(Executor):
    """Fast-path for IsValidPath — check tracker then SQLite, fall through to daemon."""

    op: ClassVar[int] = 1

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        if store.tracker.has_path(request.path):
            return IsValidPathResponse(valid=True)

        if (db := store.db) is not None:
            async with db.execute(IS_VALID_PATH, (str(request.path),)) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                store.tracker.add_known_path(request.path)
                return IsValidPathResponse(valid=True)

        return None  # fall through to daemon
