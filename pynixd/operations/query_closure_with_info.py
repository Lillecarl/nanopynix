"""QueryClosureWithInfo operation request/response types."""

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

QUERY_CLOSURE_WITH_INFO = """
WITH RECURSIVE closure(id) AS (
    SELECT id FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
    UNION
    SELECT r.reference
    FROM closure c
    JOIN Refs r ON c.id = r.referrer
)
SELECT vp.path, vp.deriver, vp.hash, vp.registrationTime, vp.narSize,
       vp.ultimate, vp.sigs, vp.ca,
       (SELECT group_concat(ref_vp.path, ' ')
        FROM Refs r
        JOIN ValidPaths ref_vp ON r.reference = ref_vp.id
        WHERE r.referrer = vp.id)
FROM closure c
JOIN ValidPaths vp ON c.id = vp.id
ORDER BY vp.id ASC
"""

log = structlog.get_logger(__name__)


@dataclass
class QueryClosureWithInfoResponse(OpResponse):
    infos: list[PathInfo] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        infos = []
        for _ in range(n):
            infos.append(await PathInfo.from_reader_keyed(reader))
        return cls(infos=infos)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.infos))
        for info in self.infos:
            await info.to_writer_keyed(writer)


@dataclass
class QueryClosureWithInfoRequest(OpRequest[QueryClosureWithInfoResponse]):
    op: ClassVar[int] = Op.QueryClosureWithInfo
    response_type: ClassVar[type[OpResponse]] = QueryClosureWithInfoResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)

    async def execute_db(self, db: LocalStoreDB) -> QueryClosureWithInfoResponse | None:
        if not self.paths:
            return QueryClosureWithInfoResponse(infos=[])

        seeds_json = json.dumps(list(self.paths))
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_CLOSURE_WITH_INFO, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()

        sorted_infos: list[PathInfo] = []
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
            sorted_infos.append(
                PathInfo(
                    path=p,
                    deriver=StorePath(deriver or ""),
                    nar_hash=nar_hash,
                    references=references,
                    registration_time=reg_time,
                    nar_size=nar_size or 0,
                    ultimate=1 if ultimate else 0,
                    sigs=set(sigs.split()) if sigs else set(),
                    ca=ca or "",
                )
            )

        return QueryClosureWithInfoResponse(infos=sorted_infos)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryClosureWithInfoResponse:
        # Try DB (via super) or existing wire connection
        result = await super().execute(store, client, suppress_last)
        if result.infos:
            store.add_known_paths({info.path for info in result.infos})
            return result

        async with store.transfer_conn() as conn:
            if "QueryClosureWithInfo" in conn.features:
                return await conn.call(self, client=client, suppress_last=suppress_last)

        all_infos: dict[StorePath, PathInfo] = {}
        pending = self.paths

        while pending:
            to_fetch = {p for p in pending if p not in all_infos}
            if not to_fetch:
                break

            from .query_path_infos import QueryPathInfosRequest

            resp = await store.execute(
                QueryPathInfosRequest(paths=to_fetch),
                client=client,
                suppress_last=suppress_last,
            )
            new_infos = resp.infos

            for p in to_fetch:
                if p not in new_infos:
                    raise ValueError(f"Path {p} not found in store closure")

            all_infos.update(new_infos)

            next_pending = set()
            for info in new_infos.values():
                for ref in info.references:
                    if ref not in all_infos:
                        next_pending.add(ref)
            pending = next_pending

        sorted_infos: list[PathInfo] = []
        visited: set[StorePath] = set()
        visiting: set[StorePath] = set()

        def visit(p: StorePath):
            if p in visited:
                return
            if p in visiting:
                return
            visiting.add(p)
            info = all_infos[p]
            for ref in info.references:
                if ref != p:
                    visit(ref)
            visiting.remove(p)
            visited.add(p)
            sorted_infos.append(info)

        for p in sorted(all_infos.keys()):
            visit(p)

        return QueryClosureWithInfoResponse(infos=sorted_infos)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QueryClosureWithInfoResponse:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        return await proxy.local_store.execute(request, client=proxy.client)
