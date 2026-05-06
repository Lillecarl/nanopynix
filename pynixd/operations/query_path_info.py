"""QueryPathInfo operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OperationLogs, OpRequest, OpResponse, UnkeyedValidPathInfo

QUERY_PATH_INFO = """
SELECT path, deriver, hash, registrationTime, narSize, ultimate, sigs, ca
FROM ValidPaths WHERE path = ?
"""

QUERY_REFERENCES = """
SELECT vp.path FROM Refs r
JOIN ValidPaths vp ON r.reference = vp.id
WHERE r.referrer = (SELECT id FROM ValidPaths WHERE path = ?)
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..wire import NixReader, NixWriter


@dataclass
class QueryPathInfoResponse(OpResponse):
    info: UnkeyedValidPathInfo | None = None

    @property
    def valid(self) -> bool:
        return self.info is not None

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        is_valid = await reader.read_uint64() != 0
        obj.info = None
        if is_valid:
            obj.info = await UnkeyedValidPathInfo.from_reader(reader)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", valid=self.valid, info=self.info)
        self.logs.to_writer(writer)
        writer.write_uint64(1 if self.valid else 0)
        if self.valid:
            assert self.info is not None
            # Explicitly use the base class method to avoid writing the path
            # (which ValidPathInfo.to_writer would do).
            UnkeyedValidPathInfo.to_writer(self.info, writer)


@dataclass
class QueryPathInfoRequest(OpRequest[QueryPathInfoResponse]):
    name: ClassVar[str] = "QueryPathInfo"
    op: ClassVar[int] = 26
    response_type: ClassVar[type[OpResponse]] = QueryPathInfoResponse
    is_query: ClassVar[bool] = True
    path: StorePath = field(default_factory=lambda: StorePath(""))

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,  # noqa: ARG003
        buffer_logs: bool = True,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.path = await reader.read_string(StorePath)
        obj.logger.debug("from_reader", path=obj.path)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathInfoResponse:
        cached = store.get_path_info(self.path)
        if cached is not None:
            store.tracker.add_known_path(self.path)
            return QueryPathInfoResponse(info=cached)

        if (db := store.db) is not None:
            async with db.execute(QUERY_PATH_INFO, (self.path,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return QueryPathInfoResponse()

            _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

            async with db.execute(QUERY_REFERENCES, (self.path,)) as cursor:
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
            store.tracker.add_known_path(self.path)
            store.add_path_info(info.with_path(self.path))
            return QueryPathInfoResponse(info=info)

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        if resp.valid:
            store.tracker.add_known_path(self.path)
            if resp.info is not None:
                store.add_path_info(resp.info.with_path(self.path))
        return resp
