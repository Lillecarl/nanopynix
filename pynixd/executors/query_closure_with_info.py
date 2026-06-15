"""Executor for QueryClosureWithInfo (op 105) — SQLite recursive CTE + JOIN, falls through to daemon."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.base import UnkeyedValidPathInfo
from pynixd.operations.query_closure_with_info import (
    QUERY_CLOSURE_WITH_INFO,
    QueryClosureWithInfoResponse,
)
from pynixd.store_path import StorePath

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryClosureWithInfoExecutor(Executor):
    """Fast-path for QueryClosureWithInfo — SQLite recursive CTE with path info, fall through to daemon."""

    op: ClassVar[int] = 105

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        if not request.paths:
            return QueryClosureWithInfoResponse(infos=[])

        if (db := store.db) is not None:
            seeds_json = json.dumps([str(p) for p in request.paths])
            async with db.execute(QUERY_CLOSURE_WITH_INFO, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()

            sorted_infos: list = []
            for (
                path,
                deriver,
                nar_hash,
                reg_time,
                nar_size,
                ultimate,
                sigs,
                ca,
                refs_str,
            ) in rows:
                p = StorePath(path)
                references = {StorePath(r) for r in refs_str.split()} if refs_str else set()
                uinfo = UnkeyedValidPathInfo(
                    deriver=StorePath(deriver or ""),
                    nar_hash=nar_hash,
                    references=references,
                    registration_time=reg_time,
                    nar_size=nar_size or 0,
                    ultimate=1 if ultimate else 0,
                    sigs=set(sigs.split()) if sigs else set(),
                    ca=ca or "",
                )
                sorted_infos.append(uinfo.with_path(p))

            store.tracker.add_known_paths({info.path for info in sorted_infos})
            store.add_path_infos(sorted_infos)
            return QueryClosureWithInfoResponse(infos=sorted_infos)

        return None  # fall through to daemon
