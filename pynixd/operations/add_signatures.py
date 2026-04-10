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
class AddSignaturesRequest(OpRequest[AddSignaturesResponse]):
    name: ClassVar[str] = "AddSignatures"
    op: ClassVar[int] = 37
    response_type: ClassVar[type[OpResponse]] = AddSignaturesResponse
    path: str = ""
    sigs: set[str] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            path=await reader.read_string(StorePath),
            sigs=await reader.read_string_set(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
        writer.write_string_set(self.sigs)
