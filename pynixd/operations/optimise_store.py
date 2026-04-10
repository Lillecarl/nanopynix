"""OptimiseStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class OptimiseStoreResponse(OpResponse):
    value: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            logs=await OperationLogs.from_reader(reader),
            value=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.value)
        self.logs.to_writer(writer)


@dataclass
class OptimiseStoreRequest(OpRequest[OptimiseStoreResponse]):
    name: ClassVar[str] = "OptimiseStore"
    op: ClassVar[int] = 34
    response_type: ClassVar[type[OpResponse]] = OptimiseStoreResponse

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls()

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
