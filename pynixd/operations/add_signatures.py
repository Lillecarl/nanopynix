"""AddSignatures operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class AddSignaturesResponse(OpResponse):
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
class AddSignaturesRequest(OpRequest[AddSignaturesResponse]):
    name: ClassVar[str] = "AddSignatures"
    op: ClassVar[int] = 37
    response_type: ClassVar[type[OpResponse]] = AddSignaturesResponse
    path: str = ""
    sigs: set[str] = field(default_factory=set)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.path = await reader.read_string(StorePath)
        self.sigs = await reader.read_string_set()
        self.logger.debug("from_reader", path=self.path, sigs=self.sigs)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)
        writer.write_string_set(self.sigs)
