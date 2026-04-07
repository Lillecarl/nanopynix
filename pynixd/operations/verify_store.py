"""VerifyStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..protocol import Op
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse


@dataclass
class VerifyStoreResponse(OpResponse):
    value: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(value=await reader.read_uint64())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.value)


@dataclass
class VerifyStoreRequest(OpRequest[VerifyStoreResponse]):
    op: ClassVar[int] = Op.VerifyStore
    response_type: ClassVar[type[OpResponse]] = VerifyStoreResponse
    check_contents: int = 0
    repair: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            check_contents=await reader.read_uint64(),
            repair=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_uint64(self.check_contents)
        writer.write_uint64(self.repair)
