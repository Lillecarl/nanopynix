"""EnsurePath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..types.context import ReadContext, WriteContext


@dataclass
class EnsurePathResponse(OpResponse):
    value: int

    # ── New-style API (ReadContext / WriteContext) ─────────────────

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", value=self.value)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(self.value)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.value = await ctx.reader.read_uint64()
        return obj


@dataclass(kw_only=True)
class EnsurePathRequest(OpRequest[EnsurePathResponse]):
    name: ClassVar[str] = "EnsurePath"
    op: ClassVar[int] = 10
    response_type: ClassVar[type[OpResponse]] = EnsurePathResponse
    path: StorePath

    # ── New-style API (ReadContext / WriteContext) ─────────────────

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.path)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.path = await ctx.reader.read_string(StorePath)
        obj.logger.debug("deserialize", path=obj.path)
        return obj
