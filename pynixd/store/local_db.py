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

    async def query_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        cached = self.get_path_info(request.path)
        if cached is not None:
            from pynixd.operations.query_path_info import QueryPathInfoResponse

            self.tracker.add_known_path(request.path)
            return QueryPathInfoResponse(info=cached)

        if self.db is not None:
            from pynixd.operations.base import UnkeyedValidPathInfo
            from pynixd.operations.query_path_info import (
                QUERY_PATH_INFO,
                QUERY_REFERENCES,
                QueryPathInfoResponse,
            )
            from pynixd.store_path import StorePath

            async with self.db.execute(QUERY_PATH_INFO, (str(request.path),)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return QueryPathInfoResponse()

            _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

            async with self.db.execute(QUERY_REFERENCES, (str(request.path),)) as cursor:
                ref_rows = await cursor.fetchall()
            refs = {r[0] for r in ref_rows}

            info = UnkeyedValidPathInfo(
                deriver=StorePath(deriver or ""),
                nar_hash=nar_hash,
                references={StorePath(r) for r in refs},
                registration_time=reg_time,
                nar_size=nar_size or 0,
                ultimate=1 if ultimate else 0,
                sigs=set(sigs.split()) if sigs else set(),
                ca=ca or "",
            )
            self.tracker.add_known_path(request.path)
            self.add_path_info(info.with_path(request.path))
            return QueryPathInfoResponse(info=info)

        return None  # fall through to DaemonStore.call()
