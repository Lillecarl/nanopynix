"""AddIndirectRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class AddIndirectRootResponse(OpResponse):
    value: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        value = await reader.read_uint64()
        return cls(
            logs=logs,
            value=value,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)


@dataclass
class AddIndirectRootRequest(OpRequest[AddIndirectRootResponse]):
    name: ClassVar[str] = "AddIndirectRoot"
    op: ClassVar[int] = 12
    response_type: ClassVar[type[OpResponse]] = AddIndirectRootResponse
    path: StorePath = StorePath("")

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        path = await reader.read_string(StorePath)
        cls.logger.debug("from_reader", path=path)
        return cls(path=path)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
