"""QuerySubstitutablePathInfos operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from ..connection import ClientConn
from .base import OpRequest, OpResponse, OperationLogs, SubstitutablePathInfo


@dataclass
class SubstitutablePathInfoEntry:
    path: str = ""
    info: SubstitutablePathInfo = field(default_factory=SubstitutablePathInfo)


@dataclass
class QuerySubstitutablePathInfosResponse(OpResponse):
    entries: list[SubstitutablePathInfoEntry] = field(default_factory=list)

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
        self.entries = []
        for _ in range(n):
            path = await reader.read_string()
            info = await SubstitutablePathInfo().from_reader(reader, version)
            self.entries.append(SubstitutablePathInfoEntry(path=path, info=info))
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", entry_count=len(self.entries))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.entries))
        for entry in self.entries:
            writer.write_string(entry.path)
            await entry.info.to_writer(writer, version)


@dataclass
class QuerySubstitutablePathInfosRequest(
    OpRequest[QuerySubstitutablePathInfosResponse]
):
    name: ClassVar[str] = "QuerySubstitutablePathInfos"
    op: ClassVar[int] = 30
    response_type: ClassVar[type[OpResponse]] = QuerySubstitutablePathInfosResponse
    is_query: ClassVar[bool] = True
    items: dict[str, str] = field(default_factory=dict)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        n = await reader.read_uint64()
        self.items = {}
        for _ in range(n):
            k = await reader.read_string()
            v = await reader.read_string()
            self.items[k] = v
        self.logger.debug("from_reader", item_count=n)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            writer.write_string(k)
            writer.write_string(v)
