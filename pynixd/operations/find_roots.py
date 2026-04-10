"""FindRoots operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


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
        return cls(logs=await OperationLogs.from_reader(reader), roots=roots)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.roots))
        for root in self.roots:
            writer.write_string(root.link)
            writer.write_string(root.target)
        self.logs.to_writer(writer)


@dataclass
class FindRootsRequest(OpRequest[FindRootsResponse]):
    name: ClassVar[str] = "FindRoots"
    op: ClassVar[int] = 14
    response_type: ClassVar[type[OpResponse]] = FindRootsResponse

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls()

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
