"""LocalDBStore — LocalStore with SQLite database for fast-path queries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .local_daemon import LocalStore

if TYPE_CHECKING:
    from ..local_store_db import LocalStoreDB
    from ..path_tracker import PathTrackerInstance


class LocalDBStore(LocalStore):
    """LocalStore with SQLite database for fast-path query optimizations.

    Owns the database and path tracker. Overrides fast-path hooks
    with SQLite implementations. Falls through to DaemonStore.call()
    when the database can't answer a query.
    """

    db: LocalStoreDB | None  # set by factory — always non-None in practice
    tracker: PathTrackerInstance

    # ── Fast-path overrides ────────────────────────────────────────

    async def is_valid_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.tracker.has_path(request.path):
            from pynixd.operations.is_valid_path import IsValidPathResponse

            return IsValidPathResponse(valid=True)

        if self.db is not None:
            from pynixd.operations.is_valid_path import IS_VALID_PATH, IsValidPathResponse

            async with self.db.execute(IS_VALID_PATH, (str(request.path),)) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                self.tracker.add_known_path(request.path)
                return IsValidPathResponse(valid=True)

        return None  # fall through to DaemonStore.call()
