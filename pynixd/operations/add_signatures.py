"""AddSignatures operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs

if TYPE_CHECKING:
    pass


@dataclass
class AddSignaturesResponse(OpResponse):
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
class AddSignaturesRequest(OpRequest[AddSignaturesResponse]):
    name: ClassVar[str] = "AddSignatures"
    op: ClassVar[int] = 37
    response_type: ClassVar[type[OpResponse]] = AddSignaturesResponse
    path: str = ""
    sigs: set[str] = field(default_factory=set)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self._read_identifier = reader.identifier
        self.path = await reader.read_string(StorePath)
        self.sigs = await reader.read_string_set()
        self.logger.debug("from_reader", path=self.path, sigs=self.sigs)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._write_identifier = writer.identifier
        writer.write_uint64(self.op)
        writer.write_string(self.path)
        writer.write_string_set(self.sigs)
