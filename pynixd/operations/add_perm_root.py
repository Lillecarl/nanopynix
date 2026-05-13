"""AddPermRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs, StderrNext
from ..types.auth import Role
from ..types.context import ReadContext, WriteContext
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..types import RequestContext as RequestContext


@dataclass
class AddPermRootResponse(OpResponse):
    gc_root: str

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.gc_root = await ctx.reader.read_string()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", gc_root=self.gc_root)
        self.logs.serialize(ctx)
        ctx.writer.write_string(self.gc_root)


@dataclass(kw_only=True)
class AddPermRootRequest(OpRequest[AddPermRootResponse]):
    name: ClassVar[str] = "AddPermRoot"
    op: ClassVar[int] = 47
    response_type: ClassVar[type[OpResponse]] = AddPermRootResponse
    store_path: str
    gc_root: str

    async def handle(self, ctx: RequestContext) -> AddPermRootResponse | None:
        self = await self.deserialize(ReadContext.from_request(ctx))
        if ctx.proxy.role == Role.ADMIN:
            return await ctx.proxy.execute(self)

        resp = AddPermRootResponse(gc_root=self.gc_root)
        msg = StderrNext(f"pynixd: AddPermRoot {self.store_path} ignored (no-op)")
        resp.logs.add(msg)
        return resp

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.store_path = await ctx.reader.read_string()
        obj.gc_root = await ctx.reader.read_string()
        obj.logger.debug(
            "deserialize",
            store_path=obj.store_path,
            gc_root=obj.gc_root,
        )
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.store_path)
        ctx.writer.write_string(self.gc_root)
