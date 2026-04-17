"""OptimiseStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs, RequestContext, Role

if TYPE_CHECKING:
    pass


@dataclass
class OptimiseStoreResponse(OpResponse):
    value: int = 0

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self._read_identifier = reader.identifier
        self.logs = await OperationLogs().from_reader(reader)
        self.value = await reader.read_uint64()
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._write_identifier = writer.identifier
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)


@dataclass
class OptimiseStoreRequest(OpRequest[OptimiseStoreResponse]):
    name: ClassVar[str] = "OptimiseStore"
    op: ClassVar[int] = 34
    response_type: ClassVar[type[OpResponse]] = OptimiseStoreResponse

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self._read_identifier = reader.identifier
        self.logger.debug("from_reader")
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._write_identifier = writer.identifier
        writer.write_uint64(self.op)

    @classmethod
    async def handle(cls, ctx: RequestContext) -> OptimiseStoreResponse | None:
        log = structlog.get_logger(f"pynixd.operations.{cls.__name__}")
        log.debug("received_op")

        # Must always consume the request to keep protocol in sync
        request = await cls().from_reader(ctx.proxy.r, ctx.version)

        if ctx.role < Role.ADMIN:
            log.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{cls.name}' requires administrative privileges."
            )
            return None

        result = await ctx.proxy.execute(request)
        log.debug("responded_op")
        return result
