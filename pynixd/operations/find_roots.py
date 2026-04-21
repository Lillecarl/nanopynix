"""FindRoots operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from ..connection import ClientConn
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class FindRootsEntry:
    link: str = ""
    target: str = ""


@dataclass
class FindRootsResponse(OpResponse):
    roots: list[FindRootsEntry] = field(default_factory=list)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(reader)
        n = await reader.read_uint64()
        self.roots = []
        for _ in range(n):
            link = await reader.read_string()
            target = await reader.read_string()
            self.roots.append(FindRootsEntry(link=link, target=target))
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", root_count=len(self.roots))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.roots))
        for root in self.roots:
            writer.write_string(root.link)
            writer.write_string(root.target)


@dataclass
class FindRootsRequest(OpRequest[FindRootsResponse]):
    name: ClassVar[str] = "FindRoots"
    op: ClassVar[int] = 14
    response_type: ClassVar[type[OpResponse]] = FindRootsResponse

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logger.debug("from_reader")
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
