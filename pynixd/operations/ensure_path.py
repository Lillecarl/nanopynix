"""EnsurePath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from ..types import OperationLogs
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class EnsurePathResponse(OpResponse):
    value: int

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
        obj.value = await reader.read_uint64()
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)


@dataclass(kw_only=True)
class EnsurePathRequest(OpRequest[EnsurePathResponse]):
    name: ClassVar[str] = "EnsurePath"
    op: ClassVar[int] = 10
    response_type: ClassVar[type[OpResponse]] = EnsurePathResponse
    path: StorePath

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.path = await reader.read_string(StorePath)
        obj.logger.debug("from_reader", path=obj.path)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)