"""AddIndirectRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse


@dataclass
class AddIndirectRootResponse(OpResponse):
    value: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(value=await reader.read_uint64())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.value)
        self.logs.to_writer(writer)


@dataclass
class AddIndirectRootRequest(OpRequest[AddIndirectRootResponse]):
    name: ClassVar[str] = "AddIndirectRoot"
    op: ClassVar[int] = 12
    response_type: ClassVar[type[OpResponse]] = AddIndirectRootResponse
    path: StorePath = StorePath("")

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(path=await reader.read_string(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
