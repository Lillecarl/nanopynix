"""LocalDBStore — LocalStore with SQLite database for fast-path queries."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import Store
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

    db: LocalStoreDB | None  # always non-None in practice after start()
    tracker: PathTrackerInstance

    async def start(self, sync_paths: bool = True) -> None:
        await super().start(sync_paths=sync_paths)
        if self.db is None:
            from ..local_store_db import LocalStoreDB
            from ..path_tracker import PathTracker

            self.db = await LocalStoreDB.open(self.store_path or Path("/"))  # type: ignore[assignment]
            path_tracker = PathTracker(db=self.db)
            self.tracker = path_tracker.create_instance(store_id=self.store_id)

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
        await super().close()

    # ── Fast-path overrides ────────────────────────────────────────

    @Store.executor(op=1)
    async def is_valid_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        path_str = str(request.path)
        from pynixd.store_path import StorePath as RealStorePath

        sp = RealStorePath(path_str)
        if self.tracker.has_path(sp):
            from pynixd.serde import IsValidPathResponse

            return IsValidPathResponse(valid=True)

        if self.db is not None:
            from pynixd.operations.is_valid_path import IS_VALID_PATH
            from pynixd.serde import IsValidPathResponse

            async with self.db.execute(IS_VALID_PATH, (path_str,)) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                self.tracker.add_known_path(sp)
                return IsValidPathResponse(valid=True)

        return None  # fall through to DaemonStore.call()

    @Store.executor(op=26)
    async def query_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        cached = self.get_path_info(request.path)
        if cached is not None:
            self.tracker.add_known_path(request.path)
            return None  # cache hit — fall through to old path until cache stores serde types

        if self.db is not None:
            from pynixd.operations.query_path_info import (
                QUERY_PATH_INFO,
                QUERY_REFERENCES,
            )
            from pynixd.serde import QueryPathInfoResponse
            from pynixd.serde import StorePath as SerdeStorePath
            from pynixd.serde.content_address import ContentAddress
            from pynixd.serde.nar_hash import NARHash
            from pynixd.serde.path_info import UnkeyedValidPathInfo as SerdeUnkeyedValidPathInfo
            from pynixd.serde.signature import Signature
            from pynixd.serde.wire_time import Time
            from pynixd.store_path import StorePath as RealStorePath

            async with self.db.execute(QUERY_PATH_INFO, (str(request.path),)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return QueryPathInfoResponse(valid=False)

            _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

            async with self.db.execute(QUERY_REFERENCES, (str(request.path),)) as cursor:
                ref_rows = await cursor.fetchall()
            refs = {r[0] for r in ref_rows}

            sig_set: set = set()
            if sigs:
                for s in sigs.split():
                    sig_set.add(Signature(s))  # type: ignore[arg-type]

            info = SerdeUnkeyedValidPathInfo(
                deriver=SerdeStorePath(path=deriver or ""),
                nar_hash=NARHash(hash=nar_hash),
                references={SerdeStorePath(path=r) for r in refs},  # type: ignore[arg-type]
                registration_time=Time(ts=reg_time),
                nar_size=nar_size or 0,
                ultimate=bool(ultimate),
                sigs=sig_set,
                ca=ContentAddress(value=ca or ""),
            )
            self.tracker.add_known_path(RealStorePath(str(request.path)))
            return QueryPathInfoResponse(valid=True, info=info)

        return None  # fall through to DaemonStore.call()

    @Store.executor(op=23)
    async def query_all_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.db is not None:
            from pynixd.operations.query_all_valid_paths import QUERY_ALL_VALID_PATHS
            from pynixd.serde import QueryAllValidPathsResponse
            from pynixd.serde import StorePath as SerdeStorePath

            try:
                async with self.db.execute(QUERY_ALL_VALID_PATHS) as cursor:
                    rows = await cursor.fetchall()
                paths: set = {SerdeStorePath(path=r[0]) for r in rows}  # type: ignore[arg-type]
                return QueryAllValidPathsResponse(paths=paths)
            except Exception:
                pass

        return None  # fall through to DaemonStore.call()

    @Store.executor(op=31)
    async def query_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.db is not None:
            import json

            from pynixd.operations.query_valid_paths import QUERY_VALID_PATHS
            from pynixd.serde import QueryValidPathsResponse
            from pynixd.serde import StorePath as SerdeStorePath

            paths_json = json.dumps([str(p) for p in request.paths])
            async with self.db.execute(QUERY_VALID_PATHS, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
            from pynixd.store_path import StorePath as RealStorePath

            paths: set = {SerdeStorePath(path=r[0]) for r in rows}  # type: ignore[arg-type]
            resp = QueryValidPathsResponse(paths=paths)
            self.tracker.add_known_paths({RealStorePath(str(p)) for p in resp.paths})
            return resp

        return None  # fall through to DaemonStore.call()

    @Store.executor(op=29)
    async def query_path_from_hash_part(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.db is not None:
            from pynixd.operations.query_path_from_hash_part import QUERY_PATH_FROM_HASH_PART
            from pynixd.serde import QueryPathFromHashPartResponse
            from pynixd.serde import StorePath as SerdeStorePath
            from pynixd.store_path import StorePath as RealStorePath

            prefix = f"/nix/store/{request.path}"
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            async with self.db.execute(QUERY_PATH_FROM_HASH_PART, (prefix, upper)) as cursor:
                row = await cursor.fetchone()
            if row:
                result = QueryPathFromHashPartResponse(value=SerdeStorePath(path=row[0]))
                self.tracker.add_known_path(RealStorePath(row[0]))
                return result

        return None  # fall through to DaemonStore.call()

    @Store.executor(op=104)
    async def query_closure(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if self.db is not None:
            import json

            from pynixd.operations.query_closure import QUERY_CLOSURE
            from pynixd.serde import QueryClosureResponse
            from pynixd.serde import StorePath as SerdeStorePath
            from pynixd.store_path import StorePath as RealStorePath

            seeds_json = json.dumps([str(p) for p in request.paths])
            async with self.db.execute(QUERY_CLOSURE, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()
            paths: set = {SerdeStorePath(path=row[0]) for row in rows}  # type: ignore[arg-type]
            result = QueryClosureResponse(paths=paths)
            self.tracker.add_known_paths({RealStorePath(str(p)) for p in result.paths})
            return result

        return None  # fall through to DaemonStore.call()

    @Store.executor(op=105)
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

    @Store.executor(op=103)
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

    @Store.executor(op=106)
    async def query_derivation_output_map_batch(
        self, request: Any, client: Any = None, suppress_last: bool = False
    ) -> Any:
        if not request.drv_paths:
            from pynixd.operations.query_derivation_output_map_batch import (
                DerivationOutputMapBatchResponse,
            )

            return DerivationOutputMapBatchResponse({})

        if self.db is not None:
            import json

            from pynixd.operations.query_derivation_output_map_batch import (
                QUERY_DERIVATION_OUTPUT_MAP_BATCH,
                DerivationOutputMapBatchResponse,
            )
            from pynixd.store_path import StorePath

            paths_json = json.dumps([str(p) for p in request.drv_paths])
            async with self.db.execute(
                QUERY_DERIVATION_OUTPUT_MAP_BATCH,
                (paths_json,),
            ) as cursor:
                rows = await cursor.fetchall()

            result: dict = {}
            for drv_path, output_name, output_path in rows:
                result.setdefault(StorePath(drv_path), {})[output_name] = (
                    StorePath(output_path) if output_path else None
                )

            for drv_path in request.drv_paths:
                if StorePath(drv_path) in result:
                    continue
                try:
                    parsed = await self.read_derivation(drv_path)
                    if parsed is None:
                        continue
                    result[StorePath(drv_path)] = dict(parsed.output_paths().items())
                except FileNotFoundError:
                    pass

            return DerivationOutputMapBatchResponse(outputs=result)

        return None  # fall through to DaemonStore.call()
