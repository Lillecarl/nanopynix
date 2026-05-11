"""QueryMissing operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import DerivedPath
from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..types.aliases import StorePathSet
    from ..types.context import ReadContext, WriteContext
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
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,  # noqa: ARG003
        buffer_logs: bool = True,  # noqa: ARG003
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

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.deserialize(ctx)
        obj.will_build = await ctx.reader.read_string_set(StorePath)
        obj.will_substitute = await ctx.reader.read_string_set(StorePath)
        obj.unknown = await ctx.reader.read_string_set(StorePath)
        obj.download_size = await ctx.reader.read_uint64()
        obj.nar_size = await ctx.reader.read_uint64()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug(
            "serialize",
            will_build=self.will_build,
            will_substitute=self.will_substitute,
            unknown=self.unknown,
        )
        self.logs.serialize(ctx)
        ctx.writer.write_string_set(self.will_build)
        ctx.writer.write_string_set(self.will_substitute)
        ctx.writer.write_string_set(self.unknown)
        ctx.writer.write_uint64(self.download_size)
        ctx.writer.write_uint64(self.nar_size)


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
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,  # noqa: ARG003
        buffer_logs: bool = True,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.derived_paths = await reader.read_string_set(DerivedPath)
        obj.logger.debug("from_reader", derived_paths=obj.derived_paths)
        return obj

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.derived_paths = await ctx.reader.read_string_set(DerivedPath)
        obj.logger.debug("deserialize", derived_paths=obj.derived_paths)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string_set(self.derived_paths)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
