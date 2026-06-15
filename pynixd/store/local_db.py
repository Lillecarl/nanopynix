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

    async def query_all_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.db is not None:
            from pynixd.operations.query_all_valid_paths import (
                QUERY_ALL_VALID_PATHS,
                QueryAllValidPathsResponse,
            )
            from pynixd.store_path import StorePath

            try:
                async with self.db.execute(QUERY_ALL_VALID_PATHS) as cursor:
                    rows = await cursor.fetchall()
                return QueryAllValidPathsResponse(
                    paths={StorePath(r[0]) for r in rows},
                )
            except Exception:
                pass

        return None  # fall through to DaemonStore.call()

    async def query_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.db is not None:
            import json

            from pynixd.operations.query_valid_paths import (
                QUERY_VALID_PATHS,
                QueryValidPathsResponse,
            )
            from pynixd.store_path import StorePath

            paths_json = json.dumps([str(p) for p in request.paths])
            async with self.db.execute(QUERY_VALID_PATHS, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
            resp = QueryValidPathsResponse(paths={StorePath(r[0]) for r in rows})
            self.tracker.add_known_paths(resp.paths)
            return resp

        return None  # fall through to DaemonStore.call()

    async def query_path_from_hash_part(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.db is not None:
            from pynixd.operations.query_path_from_hash_part import (
                QUERY_PATH_FROM_HASH_PART,
                QueryPathFromHashPartResponse,
            )
            from pynixd.store_path import StorePath

            prefix = f"/nix/store/{request.path}"
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            async with self.db.execute(QUERY_PATH_FROM_HASH_PART, (prefix, upper)) as cursor:
                row = await cursor.fetchone()
            if row:
                result = QueryPathFromHashPartResponse(value=StorePath(row[0]))
                self.tracker.add_known_path(result.value)
                return result

        return None  # fall through to DaemonStore.call()

    async def query_closure(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.db is not None:
            import json

            from pynixd.operations.query_closure import (
                QUERY_CLOSURE,
                QueryClosureResponse,
            )
            from pynixd.store_path import StorePath

            seeds_json = json.dumps([str(p) for p in request.paths])
            async with self.db.execute(QUERY_CLOSURE, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()
            result = QueryClosureResponse(paths={StorePath(row[0]) for row in rows})
            self.tracker.add_known_paths(result.paths)
            return result

        return None  # fall through to DaemonStore.call()

    async def query_closure_with_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if not request.paths:
            from pynixd.operations.query_closure_with_info import QueryClosureWithInfoResponse

            return QueryClosureWithInfoResponse(infos=[])

        if self.db is not None:
            import json

            from pynixd.operations.base import UnkeyedValidPathInfo
            from pynixd.operations.query_closure_with_info import (
                QUERY_CLOSURE_WITH_INFO,
                QueryClosureWithInfoResponse,
            )
            from pynixd.store_path import StorePath

            seeds_json = json.dumps([str(p) for p in request.paths])
            async with self.db.execute(QUERY_CLOSURE_WITH_INFO, (seeds_json,)) as cursor:
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

            self.tracker.add_known_paths({info.path for info in sorted_infos})
            self.add_path_infos(sorted_infos)
            return QueryClosureWithInfoResponse(infos=sorted_infos)

        return None  # fall through to DaemonStore.call()

    async def query_path_infos(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if not request.paths:
            from pynixd.operations.query_path_infos import QueryPathInfosResponse

            return QueryPathInfosResponse(infos={})

        cached: dict = {}
        uncached: list = []
        for path in request.paths:
            cached_info = self.get_path_info(path)
            if cached_info is not None:
                cached[path] = cached_info
            else:
                uncached.append(path)

        if not uncached:
            self.add_path_infos(cached.values())
            from pynixd.operations.query_path_infos import QueryPathInfosResponse

            return QueryPathInfosResponse(infos=cached)

        if self.db is not None:
            import json

            from pynixd.operations.base import UnkeyedValidPathInfo
            from pynixd.operations.query_path_infos import (
                QUERY_PATH_INFOS_BATCH,
                QUERY_REFERENCES_BATCH,
                QueryPathInfosResponse,
            )
            from pynixd.store_path import StorePath

            paths_json = json.dumps([str(p) for p in uncached])
            async with self.db.execute(QUERY_PATH_INFOS_BATCH, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
            async with self.db.execute(QUERY_REFERENCES_BATCH, (paths_json,)) as cursor:
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

            self.tracker.add_known_paths(set(infos.keys()))
            self.add_path_infos(infos.values())
            return QueryPathInfosResponse(infos=infos)

        return None  # fall through to DaemonStore.call()
