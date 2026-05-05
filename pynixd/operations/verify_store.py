"""VerifyStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self


from .base import OpRequest, OpResponse, OperationLogs, RequestContext, Role
if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class VerifyStoreResponse(OpResponse):
    value: int

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(reader, client=client, buffer=buffer_logs)
        obj.value = await reader.read_uint64()
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)


@dataclass(kw_only=True)
class VerifyStoreRequest(OpRequest[VerifyStoreResponse]):
    name: ClassVar[str] = "VerifyStore"
    op: ClassVar[int] = 35
    response_type: ClassVar[type[OpResponse]] = VerifyStoreResponse
    check_contents: int
    repair: int

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.check_contents = await reader.read_uint64()
        obj.repair = await reader.read_uint64()
        obj.logger.debug(
            "from_reader",
            check_contents=obj.check_contents,
            repair=obj.repair,
        )
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_uint64(self.check_contents)
        writer.write_uint64(self.repair)

    async def handle(self, ctx: RequestContext) -> VerifyStoreResponse | None:
        self.logger.debug("received_op")

        # Must always consume the request to keep protocol in sync
        self = await self.from_reader(ctx.proxy.r, ctx.version)

        if ctx.role < Role.ADMIN:
            self.logger.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{self.name}' requires administrative privileges.",
            )
            return None

        result = await ctx.proxy.execute(self)
        self.logger.debug("responded_op")
        return result
