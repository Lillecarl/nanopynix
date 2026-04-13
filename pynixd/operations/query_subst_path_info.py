"""QuerySubstitutablePathInfo operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs, SubstitutablePathInfo


@dataclass
class QuerySubstitutablePathInfoResponse(OpResponse):
    found: bool = False
    info: SubstitutablePathInfo | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        found = await reader.read_uint64() != 0
        info = None
        if found:
            info = await SubstitutablePathInfo.from_reader(reader, version)
        return cls(logs=logs, found=found, info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", found=self.found)
        self.logs.to_writer(writer)
        writer.write_uint64(1 if self.found else 0)
        if self.found and self.info is not None:
            await self.info.to_writer(writer, version)


@dataclass
class QuerySubstitutablePathInfoRequest(OpRequest[QuerySubstitutablePathInfoResponse]):
    name: ClassVar[str] = "QuerySubstitutablePathInfo"
    op: ClassVar[int] = 21
    response_type: ClassVar[type[OpResponse]] = QuerySubstitutablePathInfoResponse
    is_query: ClassVar[bool] = True
    path: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        path = await reader.read_string()
        cls.logger.debug("from_reader", path=path)
        return cls(path=path)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
