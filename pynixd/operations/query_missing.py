"""QueryMissing operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import DerivedPath
from ..store_path import StorePath
from ..types import OperationLogs
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..types.aliases import StorePathSet
    from ..wire import NixReader, NixWriter


@dataclass
class QueryMissingResponse(OpResponse):
    will_build: StorePathSet
    will_substitute: StorePathSet
    unknown: StorePathSet
    download_size: int
    nar_size: int

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
        obj.will_build = await reader.read_string_set(StorePath)
        obj.will_substitute = await reader.read_string_set(StorePath)
        obj.unknown = await reader.read_string_set(StorePath)
        obj.download_size = await reader.read_uint64()
        obj.nar_size = await reader.read_uint64()
        return obj

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


@dataclass(kw_only=True)
class QueryMissingRequest(OpRequest[QueryMissingResponse]):
    name: ClassVar[str] = "QueryMissing"
    op: ClassVar[int] = 40
    response_type: ClassVar[type[OpResponse]] = QueryMissingResponse
    is_query: ClassVar[bool] = True
    derived_paths: set[DerivedPath]

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
        obj.derived_paths = await reader.read_string_set(DerivedPath)
        obj.logger.debug("from_reader", derived_paths=obj.derived_paths)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
