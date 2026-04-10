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

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        items: dict[str, StorePath] = {}
        for _ in range(n):
            k = await reader.read_string()
            v = await reader.read_string(StorePath)
            items[k] = v
        return cls(logs=await OperationLogs.from_reader(reader), items=items)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            writer.write_string(k)
            writer.write_string(v)
        self.logs.to_writer(writer)


@dataclass
class QueryDerivationOutputMapRequest(OpRequest[QueryDerivationOutputMapResponse]):
    name: ClassVar[str] = "QueryDerivationOutputMap"
    op: ClassVar[int] = 41
    response_type: ClassVar[type[OpResponse]] = QueryDerivationOutputMapResponse
    is_query: ClassVar[bool] = True
    path: StorePath = StorePath("")

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(path=await reader.read_string(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
