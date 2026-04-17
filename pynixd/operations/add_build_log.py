"""AddBuildLog operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs, RequestContext, Role

if TYPE_CHECKING:
    pass


@dataclass
class AddBuildLogResponse(OpResponse):
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
class AddBuildLogRequest(OpRequest[AddBuildLogResponse]):
    name: ClassVar[str] = "AddBuildLog"
    op: ClassVar[int] = 45
    response_type: ClassVar[type[OpResponse]] = AddBuildLogResponse
    path: StorePath = StorePath("")

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self._read_identifier = reader.identifier
        self.path = await reader.read_string(StorePath)
        self.logger.debug("from_reader", path=self.path)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._write_identifier = writer.identifier
        writer.write_uint64(self.op)
        writer.write_string(self.path)

    @classmethod
    async def handle(cls, ctx: RequestContext) -> AddBuildLogResponse | None:
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
