"""QueryPathInfo operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpResponse, PathInfo, SingleStringRequest

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..store import Store

QUERY_PATH_INFO = """
SELECT path, deriver, hash, registrationTime, narSize, ultimate, sigs, ca
FROM ValidPaths WHERE path = ?
"""

QUERY_REFERENCES = """
SELECT vp.path FROM Refs r
JOIN ValidPaths vp ON r.reference = vp.id
WHERE r.referrer = (SELECT id FROM ValidPaths WHERE path = ?)
"""


@dataclass
class QueryPathInfoResponse(OpResponse):
    valid: bool = False
    info: PathInfo | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        valid = await reader.read_uint64() != 0
        info = None
        if valid:
            info = await PathInfo.from_reader_unkeyed(reader)
        return cls(valid=valid, info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(1 if self.valid else 0)
        if self.valid and self.info is not None:
            await self.info.to_writer_unkeyed(writer)


@dataclass
class QueryPathInfoRequest(SingleStringRequest[QueryPathInfoResponse]):
    op: ClassVar[int] = Op.QueryPathInfo
    response_type: ClassVar[type[OpResponse]] = QueryPathInfoResponse
    is_query: ClassVar[bool] = True

    async def execute_db(self, db: LocalStoreDB) -> QueryPathInfoResponse | None:
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_PATH_INFO, (self.path,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return QueryPathInfoResponse(valid=False)

            _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

            async with conn.execute(QUERY_REFERENCES, (self.path,)) as cursor:
                ref_rows = await cursor.fetchall()
            refs = {r[0] for r in ref_rows}

        return QueryPathInfoResponse(
            valid=True,
            info=PathInfo(
                path=self.path,
                deriver=StorePath(deriver or ""),
                nar_hash=nar_hash,
                references={StorePath(r) for r in refs},
                registration_time=reg_time,
                nar_size=nar_size or 0,
                ultimate=1 if ultimate else 0,
                sigs=set(sigs.split()) if sigs else set(),
                ca=ca or "",
            ),
        )

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathInfoResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None and result.valid:
                store.add_known_path(self.path)
                return result

        resp = await super().execute(store, client, suppress_last)
        if resp.valid:
            store.add_known_path(self.path)
            if resp.info is not None:
                resp.info.path = self.path
        return resp
