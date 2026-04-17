"""QueryPathInfos operation request/response types. This is a custom operation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self


from ..exceptions import OpNotImplementedError
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import (
    OpRequest,
    OpResponse,
    OperationLogs,
    ValidPathInfo,
    UnkeyedValidPathInfo,
)
from .query_path_info import QueryPathInfoRequest

QUERY_PATH_INFOS_BATCH = """
SELECT vp.path, vp.deriver, vp.hash, registrationTime, narSize,
       vp.ultimate, vp.sigs, vp.ca
FROM ValidPaths vp
WHERE vp.path IN (SELECT value FROM json_each(?))
"""

QUERY_REFERENCES_BATCH = """
SELECT vp_referrer.path, vp_ref.path
FROM Refs r
JOIN ValidPaths vp_referrer ON r.referrer = vp_referrer.id
JOIN ValidPaths vp_ref ON r.reference = vp_ref.id
WHERE vp_referrer.path IN (SELECT value FROM json_each(?))
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class QueryPathInfosResponse(OpResponse):
    infos: dict[StorePath, ValidPathInfo] = field(default_factory=dict)

    @property
    def is_not_found(self) -> bool:
        return not self.infos

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(reader)
        n = await reader.read_uint64()
        self.infos = {}
        for _ in range(n):
            info = await ValidPathInfo().from_reader(reader)
            self.infos[info.path] = info
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", info_count=len(self.infos))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.infos))
        for info in self.infos.values():
            info.to_writer(writer)


@dataclass
class QueryPathInfosRequest(OpRequest[QueryPathInfosResponse]):
    name: ClassVar[str] = "QueryPathInfos"
    op: ClassVar[int] = 103
    is_extension: ClassVar[bool] = True
    response_type: ClassVar[type[OpResponse]] = QueryPathInfosResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.paths = await reader.read_string_set(StorePath)
        self.logger.debug("from_reader", paths=self.paths)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathInfosResponse:
        if not self.paths:
            return QueryPathInfosResponse(infos={})

        cached: dict[StorePath, ValidPathInfo] = {}
        uncached: list[StorePath] = []
        for path in self.paths:
            cached_info = store.get_path_info(path)
            if cached_info is not None:
                cached[path] = cached_info
            else:
                uncached.append(path)

        if not uncached:
            store.add_path_infos(cached.values())
            return QueryPathInfosResponse(infos=cached)

        infos: dict[StorePath, ValidPathInfo] = dict(cached)

        if (db := store.native_db) is not None:
            paths_json = json.dumps([str(p) for p in uncached])
            async with db.execute(QUERY_PATH_INFOS_BATCH, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
            async with db.execute(QUERY_REFERENCES_BATCH, (paths_json,)) as cursor:
                ref_rows = await cursor.fetchall()

            refs_map: dict[StorePath, set[StorePath]] = {}
            for referrer, reference in ref_rows:
                refs_map.setdefault(StorePath(referrer), set()).add(
                    StorePath(reference)
                )

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

        # Try delegation via wire (if talking to another pynixd)
        try:
            return await super().execute(store, client, suppress_last)
        except OpNotImplementedError:
            pass  # Backend doesn't support the extension, fall back to decomposition

        for path in uncached:
            resp = await store.execute(
                QueryPathInfoRequest(path=path),
                client=client,
                suppress_last=suppress_last,
            )
            if resp.valid and resp.info:
                vinfo = resp.info.with_path(path)
                infos[path] = vinfo
                store.add_path_info(vinfo)
        return QueryPathInfosResponse(infos=infos)
