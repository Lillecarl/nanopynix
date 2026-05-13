"""IsValidPath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.context import ReadContext, WriteContext

IS_VALID_PATH = "SELECT 1 FROM ValidPaths WHERE path = ? LIMIT 1"


@dataclass
class IsValidPathResponse(OpResponse):
    valid: bool

    # ── New-style API (ReadContext / WriteContext) ──────────────

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.valid = await ctx.reader.read_uint64() != 0
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", valid=self.valid)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(1 if self.valid else 0)


@dataclass(kw_only=True)
class IsValidPathRequest(OpRequest[IsValidPathResponse]):
    name: ClassVar[str] = "IsValidPath"
    op: ClassVar[int] = 1
    response_type: ClassVar[type[OpResponse]] = IsValidPathResponse
    is_query: ClassVar[bool] = True
    path: StorePath

    # ── New-style API (ReadContext / WriteContext) ──────────────

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
    ) -> IsValidPathResponse:
        if store.tracker.has_path(self.path):
            return IsValidPathResponse(valid=True)

        if (db := store.db) is not None:
            async with db.execute(IS_VALID_PATH, (self.path,)) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                store.tracker.add_known_path(self.path)
                return IsValidPathResponse(valid=True)

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        if resp.valid:
            store.tracker.add_known_path(self.path)
        return resp
