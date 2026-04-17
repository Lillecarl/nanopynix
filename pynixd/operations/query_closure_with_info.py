"""QueryClosureWithInfo operation request/response types. This is a custom operation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

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
from .query_path_infos import QueryPathInfosRequest

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

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class QueryClosureWithInfoResponse(OpResponse):
    infos: list[ValidPathInfo] = field(default_factory=list)

    @property
    def is_not_found(self) -> bool:
        return not self.infos

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self._read_identifier = reader.identifier
        self.logs = await OperationLogs().from_reader(reader)
        n = await reader.read_uint64()
        self.infos = []
        for _ in range(n):
            self.infos.append(await ValidPathInfo().from_reader(reader))
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._write_identifier = writer.identifier
        self.logger.debug("to_writer", info_count=len(self.infos))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.infos))
        for info in self.infos:
            info.to_writer(writer)


@dataclass
class QueryClosureWithInfoRequest(OpRequest[QueryClosureWithInfoResponse]):
    name: ClassVar[str] = "QueryClosureWithInfo"
    op: ClassVar[int] = 105
    is_extension: ClassVar[bool] = True
    response_type: ClassVar[type[OpResponse]] = QueryClosureWithInfoResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self._read_identifier = reader.identifier
        self.paths = await reader.read_string_set(StorePath)
        self.logger.debug("from_reader", paths=self.paths)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._write_identifier = writer.identifier
        writer.write_uint64(self.op)
        writer.write_string_set(self.paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryClosureWithInfoResponse:
        if not self.paths:
            return QueryClosureWithInfoResponse(infos=[])

        if (db := store.native_db) is not None:
            seeds_json = json.dumps([str(p) for p in self.paths])
            async with db.execute(QUERY_CLOSURE_WITH_INFO, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()

            sorted_infos: list[ValidPathInfo] = []
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
                references = (
                    {StorePath(r) for r in refs_str.split()} if refs_str else set()
                )
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

        # Try delegation via wire (if talking to another pynixd)
        try:
            return await super().execute(store, client, suppress_last)
        except OpNotImplementedError:
            pass  # Backend doesn't support the extension, fall back to manual walk

        all_infos: dict[StorePath, ValidPathInfo] = {}
        pending = self.paths

        while pending:
            to_fetch = {p for p in pending if p not in all_infos}
            if not to_fetch:
                break

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

        sorted_infos: list[ValidPathInfo] = []
        visited: set[StorePath] = set()
        visiting: set[StorePath] = set()

        def visit(p: StorePath) -> None:
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
