"""QuerySubstitutablePaths operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OperationLogs, OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class QuerySubstitutablePathsResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(reader)
        self.paths = await reader.read_string_set(StorePath)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", paths=self.paths)
        self.logs.to_writer(writer)
        writer.write_string_set(self.paths)


@dataclass
class QuerySubstitutablePathsRequest(OpRequest[QuerySubstitutablePathsResponse]):
    name: ClassVar[str] = "QuerySubstitutablePaths"
    op: ClassVar[int] = 32
    response_type: ClassVar[type[OpResponse]] = QuerySubstitutablePathsResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.paths = await reader.read_string_set(StorePath)
        self.logger.debug("from_reader", paths=self.paths)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.paths)
