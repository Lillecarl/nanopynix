"""QueryDerivationOutputMap operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class QueryDerivationOutputMapResponse(OpResponse):
    items: dict[str, StorePath] = field(default_factory=dict)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self._read_identifier = reader.identifier
        self.logs = await OperationLogs().from_reader(reader)
        n = await reader.read_uint64()
        self.items = {}
        for _ in range(n):
            k = await reader.read_string()
            v = await reader.read_string(StorePath)
            self.items[k] = v
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._write_identifier = writer.identifier
        self.logger.debug("to_writer", item_count=len(self.items))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            writer.write_string(k)
            writer.write_string(v)


@dataclass
class QueryDerivationOutputMapRequest(OpRequest[QueryDerivationOutputMapResponse]):
    name: ClassVar[str] = "QueryDerivationOutputMap"
    op: ClassVar[int] = 41
    response_type: ClassVar[type[OpResponse]] = QueryDerivationOutputMapResponse
    is_query: ClassVar[bool] = True
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
