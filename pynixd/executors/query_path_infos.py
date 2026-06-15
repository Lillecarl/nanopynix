"""Executor for QueryPathInfos (op 103) — SQLite batch fast-path, falls through to daemon."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.base import UnkeyedValidPathInfo
from pynixd.operations.query_path_infos import (
    QUERY_PATH_INFOS_BATCH,
    QUERY_REFERENCES_BATCH,
    QueryPathInfosResponse,
)
from pynixd.store_path import StorePath

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryPathInfosExecutor(Executor):
    """Fast-path for QueryPathInfos — SQLite batch with reference map composition, falls through to daemon."""

    op: ClassVar[int] = 103

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        if not request.paths:
            return QueryPathInfosResponse(infos={})

        cached: dict = {}
        uncached: list = []
        for path in request.paths:
            cached_info = store.get_path_info(path)
            if cached_info is not None:
                cached[path] = cached_info
            else:
                uncached.append(path)

        if not uncached:
            store.add_path_infos(cached.values())
            return QueryPathInfosResponse(infos=cached)

        if (db := store.db) is not None:
            paths_json = json.dumps([str(p) for p in uncached])
            async with db.execute(QUERY_PATH_INFOS_BATCH, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
            async with db.execute(QUERY_REFERENCES_BATCH, (paths_json,)) as cursor:
                ref_rows = await cursor.fetchall()

            refs_map: dict = {}
            for referrer, reference in ref_rows:
                refs_map.setdefault(StorePath(referrer), set()).add(
                    StorePath(reference),
                )

            infos: dict = dict(cached)
            for path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca in rows:
                p = StorePath(path)
                uinfo = UnkeyedValidPathInfo(
                    deriver=StorePath(deriver or ""),
                    nar_hash=nar_hash,
                    references=refs_map.get(p, set()),
                    registration_time=reg_time,
                    nar_size=nar_size or 0,
                    ultimate=1 if ultimate else 0,
                    sigs=set(sigs.split()) if sigs else set(),
                    ca=ca or "",
                )
                infos[p] = uinfo.with_path(p)

            store.tracker.add_known_paths(set(infos.keys()))
            store.add_path_infos(infos.values())
            return QueryPathInfosResponse(infos=infos)

        return None  # fall through to daemon
