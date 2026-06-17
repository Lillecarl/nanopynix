"""QueryPathInfo operation request/response types."""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OperationLogs, OpRequest, OpResponse, UnkeyedValidPathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.context import ReadContext, WriteContext

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
    info: UnkeyedValidPathInfo | None = None

    @property
    def valid(self) -> bool:
        return self.info is not None

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        is_valid = await ctx.reader.read_uint64() != 0
        obj.info = None
        if is_valid:
            obj.info = await UnkeyedValidPathInfo.deserialize(ctx)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", valid=self.valid, info=self.info)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(1 if self.valid else 0)
        if self.valid:
            if self.info is None:
                raise RuntimeError("info is None for valid path")
            UnkeyedValidPathInfo.serialize(self.info, ctx)


@dataclass
class QueryPathInfoRequest(OpRequest[QueryPathInfoResponse]):
    name: ClassVar[str] = "QueryPathInfo"
    op: ClassVar[int] = 26
    response_type: ClassVar[type[OpResponse]] = QueryPathInfoResponse
    is_query: ClassVar[bool] = True
    path: StorePath = field(default_factory=lambda: StorePath(""))

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.path = await ctx.reader.read_string(StorePath)
        obj.logger.debug("deserialize", path=obj.path)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.path)

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
            async with db.execute(QUERY_PATH_INFO, (str(self.path),)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return QueryPathInfoResponse()

            _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

            async with db.execute(QUERY_REFERENCES, (str(self.path),)) as cursor:
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
