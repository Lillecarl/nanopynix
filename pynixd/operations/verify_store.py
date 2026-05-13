"""VerifyStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .base import OperationLogs, OpRequest, OpResponse, Role

if TYPE_CHECKING:
    from ..types import RequestContext as RequestContext

from ..types.context import ReadContext, WriteContext


@dataclass
class VerifyStoreResponse(OpResponse):
    value: int

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.deserialize(ctx)
        obj.value = await ctx.reader.read_uint64()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", value=self.value)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(self.value)


@dataclass(kw_only=True)
class VerifyStoreRequest(OpRequest[VerifyStoreResponse]):
    name: ClassVar[str] = "VerifyStore"
    op: ClassVar[int] = 35
    response_type: ClassVar[type[OpResponse]] = VerifyStoreResponse
    check_contents: int
    repair: int

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.check_contents = await ctx.reader.read_uint64()
        obj.repair = await ctx.reader.read_uint64()
        obj.logger.debug(
            "deserialize",
            check_contents=obj.check_contents,
            repair=obj.repair,
        )
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_uint64(self.check_contents)
        ctx.writer.write_uint64(self.repair)

    async def handle(self, ctx: RequestContext) -> VerifyStoreResponse | None:
        self.logger.debug("received_op")

        # Must always consume the request to keep protocol in sync
        r_ctx = ReadContext(reader=ctx.proxy.r, version=ctx.version)
        self = await self.deserialize(r_ctx)

        if ctx.role < Role.ADMIN:
            self.logger.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{self.name}' requires administrative privileges.",
            )
            return None

        result = await ctx.proxy.execute(self)
        self.logger.debug("responded_op")
        return result
