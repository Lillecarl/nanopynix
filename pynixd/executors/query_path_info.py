"""Executor for QueryPathInfo (op 26) — cache + SQLite fast-path, falls through to daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.base import UnkeyedValidPathInfo
from pynixd.operations.query_path_info import (
    QUERY_PATH_INFO,
    QUERY_REFERENCES,
    QueryPathInfoResponse,
)
from pynixd.store_path import StorePath

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryPathInfoExecutor(Executor):
    """Fast-path for QueryPathInfo — cache check + SQLite path info + references, falls through to daemon."""

    op: ClassVar[int] = 26

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        cached = store.get_path_info(request.path)
        if cached is not None:
            store.tracker.add_known_path(request.path)
            return QueryPathInfoResponse(info=cached)

        if (db := store.db) is not None:
            async with db.execute(QUERY_PATH_INFO, (str(request.path),)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return QueryPathInfoResponse()

            _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

            async with db.execute(QUERY_REFERENCES, (str(request.path),)) as cursor:
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
            store.tracker.add_known_path(request.path)
            store.add_path_info(info.with_path(request.path))
            return QueryPathInfoResponse(info=info)

        return None  # fall through to daemon
