"""FindRoots operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..protocol import Op
from ..wire import NixReader, NixWriter
from .base import EmptyRequest, OpResponse


@dataclass
class FindRootsEntry:
    link: str = ""
    target: str = ""


@dataclass
class FindRootsResponse(OpResponse):
    roots: list[FindRootsEntry] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        roots = []
        for _ in range(n):
            link = await reader.read_string()
            target = await reader.read_string()
            roots.append(FindRootsEntry(link=link, target=target))
        return cls(roots=roots)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.roots))
        for root in self.roots:
            writer.write_string(root.link)
            writer.write_string(root.target)


@dataclass
class FindRootsRequest(EmptyRequest[FindRootsResponse]):
    op: ClassVar[int] = Op.FindRoots
    response_type: ClassVar[type[OpResponse]] = FindRootsResponse
