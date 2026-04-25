"""QuerySubstitutablePathInfo operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..connection import ClientConn
from ..wire import NixReader, NixWriter
from .base import OperationLogs, OpRequest, OpResponse, SubstitutablePathInfo


@dataclass
class QuerySubstitutablePathInfoResponse(OpResponse):
    found: bool = False
    info: SubstitutablePathInfo | None = None

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(reader)
        self.found = await reader.read_uint64() != 0
        self.info = None
        if self.found:
            self.info = await SubstitutablePathInfo().from_reader(reader, version)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
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

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.path = await reader.read_string()
        self.logger.debug("from_reader", path=self.path)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)
