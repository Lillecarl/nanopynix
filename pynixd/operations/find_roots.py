"""FindRoots operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..types import OperationLogs
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class FindRootsEntry:
    link: str
    target: str


@dataclass
class FindRootsResponse(OpResponse):
    roots: list[FindRootsEntry]

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(reader)
        n = await reader.read_uint64()
        obj.roots = []
        for _ in range(n):
            link = await reader.read_string()
            target = await reader.read_string()
            obj.roots.append(FindRootsEntry(link=link, target=target))
        return obj

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

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logger.debug("from_reader")
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)