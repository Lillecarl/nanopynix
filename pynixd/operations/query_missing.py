"""QueryMissing operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..connection import ClientConn
from ..derived_path import DerivedPath
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OperationLogs, OpRequest, OpResponse


@dataclass
class QueryMissingResponse(OpResponse):
    will_build: set[StorePath] = field(default_factory=set)
    will_substitute: set[StorePath] = field(default_factory=set)
    unknown: set[StorePath] = field(default_factory=set)
    download_size: int = 0
    nar_size: int = 0

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(reader)
        self.will_build = await reader.read_string_set(StorePath)
        self.will_substitute = await reader.read_string_set(StorePath)
        self.unknown = await reader.read_string_set(StorePath)
        self.download_size = await reader.read_uint64()
        self.nar_size = await reader.read_uint64()
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug(
            "to_writer",
            will_build=self.will_build,
            will_substitute=self.will_substitute,
            unknown=self.unknown,
        )
        self.logs.to_writer(writer)
        writer.write_string_set(self.will_build)
        writer.write_string_set(self.will_substitute)
        writer.write_string_set(self.unknown)
        writer.write_uint64(self.download_size)
        writer.write_uint64(self.nar_size)


@dataclass
class QueryMissingRequest(OpRequest[QueryMissingResponse]):
    name: ClassVar[str] = "QueryMissing"
    op: ClassVar[int] = 40
    response_type: ClassVar[type[OpResponse]] = QueryMissingResponse
    is_query: ClassVar[bool] = True
    derived_paths: set[DerivedPath] = field(default_factory=set)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.derived_paths = await reader.read_string_set(DerivedPath)
        self.logger.debug("from_reader", derived_paths=self.derived_paths)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
