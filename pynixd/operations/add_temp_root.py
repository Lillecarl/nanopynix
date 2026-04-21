"""AddTempRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn


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
        await self.logs.from_reader(reader, client=client, buffer=buffer_logs)
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
    path: StorePath = StorePath("")

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.path = await reader.read_string(StorePath)
        self.logger.debug("from_reader", path=self.path)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)
