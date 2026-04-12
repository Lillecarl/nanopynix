"""QuerySubstitutablePathInfos operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs, SubstPathInfo


@dataclass
class SubstPathInfoEntry:
    path: str = ""
    info: SubstPathInfo = field(default_factory=SubstPathInfo)


@dataclass
class QuerySubstPathInfosResponse(OpResponse):
    entries: list[SubstPathInfoEntry] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        n = await reader.read_uint64()
        entries = []
        for _ in range(n):
            path = await reader.read_string()
            info = await SubstPathInfo.from_reader(reader, version)
            entries.append(SubstPathInfoEntry(path=path, info=info))
        return cls(logs=logs, entries=entries)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", entry_count=len(self.entries))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.entries))
        for entry in self.entries:
            writer.write_string(entry.path)
            await entry.info.to_writer(writer, version)


@dataclass
class QuerySubstPathInfosRequest(OpRequest[QuerySubstPathInfosResponse]):
    name: ClassVar[str] = "QuerySubstitutablePathInfos"
    op: ClassVar[int] = 30
    response_type: ClassVar[type[OpResponse]] = QuerySubstPathInfosResponse
    is_query: ClassVar[bool] = True
    items: dict[str, str] = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        items: dict[str, str] = {}
        for _ in range(n):
            k = await reader.read_string()
            v = await reader.read_string()
            items[k] = v
        cls.logger.debug("from_reader", item_count=n)
        return cls(items=items)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            writer.write_string(k)
            writer.write_string(v)
