"""AddSignatures operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, Uint64Response


@dataclass
class AddSignaturesRequest(OpRequest[Uint64Response]):
    op: ClassVar[int] = Op.AddSignatures
    response_type: ClassVar[type[OpResponse]] = Uint64Response
    path: str = ""
    sigs: set[str] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            path=await reader.read_string(StorePath),
            sigs=await reader.read_string_set(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.path)
        writer.write_string_set(self.sigs)
