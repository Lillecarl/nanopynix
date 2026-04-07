"""QueryPathInfos operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, PathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..proxy import DaemonProxy
    from ..store import Store

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

log = structlog.get_logger(__name__)


@dataclass
class QueryPathInfosResponse(OpResponse):
    infos: dict[StorePath, PathInfo] = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        infos = {}
        for _ in range(n):
            info = await PathInfo.from_reader_keyed(reader)
            infos[info.path] = info
        return cls(infos=infos)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.infos))
        for info in self.infos.values():
            await info.to_writer_keyed(writer)


@dataclass
class QueryPathInfosRequest(OpRequest[QueryPathInfosResponse]):
    op: ClassVar[int] = Op.QueryPathInfos
    response_type: ClassVar[type[OpResponse]] = QueryPathInfosResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)

    async def execute_db(self, db: LocalStoreDB) -> QueryPathInfosResponse | None:
        if not self.paths:
            return QueryPathInfosResponse(infos={})

        paths_json = json.dumps(list(self.paths))
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_PATH_INFOS_BATCH, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
            async with conn.execute(QUERY_REFERENCES_BATCH, (paths_json,)) as cursor:
                ref_rows = await cursor.fetchall()

        refs_map: dict[StorePath, set[StorePath]] = {}
        for referrer, reference in ref_rows:
            refs_map.setdefault(StorePath(referrer), set()).add(StorePath(reference))

        infos: dict[StorePath, PathInfo] = {}
        for path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca in rows:
            p = StorePath(path)
            infos[p] = PathInfo(
                path=p,
                deriver=StorePath(deriver or ""),
                nar_hash=nar_hash,
                references=refs_map.get(p, set()),
                registration_time=reg_time,
                nar_size=nar_size or 0,
                ultimate=1 if ultimate else 0,
                sigs=set(sigs.split()) if sigs else set(),
                ca=ca or "",
            )
        return QueryPathInfosResponse(infos=infos)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathInfosResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None:
                store.add_known_paths(set(result.infos.keys()))
                return result

        async with store.transfer_conn() as conn:
            if "QueryPathInfos" in conn.features:
                return await conn.call(self, client=client, suppress_last=suppress_last)

        infos: dict[StorePath, PathInfo] = {}
        for path in self.paths:
            from .query_path_info import QueryPathInfoRequest

            resp = await store.execute(
                QueryPathInfoRequest(path=path),
                client=client,
                suppress_last=suppress_last,
            )
            if resp.valid and resp.info:
                infos[path] = resp.info
        return QueryPathInfosResponse(infos=infos)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QueryPathInfosResponse:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        return await proxy.local_store.execute(request, client=proxy.client)
