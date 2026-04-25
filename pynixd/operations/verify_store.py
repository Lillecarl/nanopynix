"""VerifyStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, RequestContext, Role

if TYPE_CHECKING:
    from ..connection import ClientConn


@dataclass
class VerifyStoreResponse(OpResponse):
    value: int = 0

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(reader, client=client, buffer=buffer_logs)
        self.value = await reader.read_uint64()
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)


@dataclass
class VerifyStoreRequest(OpRequest[VerifyStoreResponse]):
    name: ClassVar[str] = "VerifyStore"
    op: ClassVar[int] = 35
    response_type: ClassVar[type[OpResponse]] = VerifyStoreResponse
    check_contents: int = 0
    repair: int = 0

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.check_contents = await reader.read_uint64()
        self.repair = await reader.read_uint64()
        self.logger.debug(
            "from_reader",
            check_contents=self.check_contents,
            repair=self.repair,
        )
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_uint64(self.check_contents)
        writer.write_uint64(self.repair)

    async def handle(self, ctx: RequestContext) -> VerifyStoreResponse | None:
        self.logger.debug("received_op")

        # Must always consume the request to keep protocol in sync
        await self.from_reader(ctx.proxy.r, ctx.version)

        if ctx.role < Role.ADMIN:
            self.logger.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{self.name}' requires administrative privileges.",
            )
            return None

        result = await ctx.proxy.execute(self)
        self.logger.debug("responded_op")
        return result
