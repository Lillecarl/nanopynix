"""AddTempRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import StderrNext
from ..store_path import StorePath
from ..types.auth import Role
from .base import OperationLogs, OpRequest, OpResponse, RequestContext

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class AddTempRootResponse(OpResponse):
    value: int = 0

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(

            reader,
            client=client,
            buffer=buffer_logs,
        )
        self.value = await reader.read_uint64()
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logs.to_writer(writer)
        self.logger.debug("to_writer", value=self.value)
        writer.write_uint64(self.value)


@dataclass
class AddTempRootRequest(OpRequest[AddTempRootResponse]):
    name: ClassVar[str] = "AddTempRoot"
    op: ClassVar[int] = 11
    response_type: ClassVar[type[OpResponse]] = AddTempRootResponse
    path: StorePath = field(default_factory=lambda: StorePath(""))

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.path = await reader.read_string(StorePath)
        self.logger.debug("from_reader", path=self.path)
        return self

    async def handle(self, ctx: RequestContext) -> AddTempRootResponse | None:
        await self.from_reader(ctx.proxy.r, ctx.version)
        if ctx.proxy.role == Role.ADMIN:
            return await ctx.proxy.execute(self)

        resp = AddTempRootResponse(value=1)
        msg = StderrNext(f"pynixd: AddTempRoot {self.path} ignored (no-op)")
        resp.logs.add(msg)
        return resp

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)
