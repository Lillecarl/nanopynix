"""QueryMissing operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..derived_path import DerivedPath
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class QueryMissingResponse(OpResponse):
    will_build: set[StorePath] = field(default_factory=set)
    will_substitute: set[StorePath] = field(default_factory=set)
    unknown: set[StorePath] = field(default_factory=set)
    download_size: int = 0
    nar_size: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            logs=await OperationLogs.from_reader(reader),
            will_build=await reader.read_string_set(StorePath),
            will_substitute=await reader.read_string_set(StorePath),
            unknown=await reader.read_string_set(StorePath),
            download_size=await reader.read_uint64(),
            nar_size=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
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

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        derived_paths = await reader.read_string_set(DerivedPath)
        cls.logger.debug("from_reader", derived_paths=derived_paths)
        return cls(derived_paths=derived_paths)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
