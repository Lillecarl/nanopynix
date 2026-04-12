"""AddTempRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class AddTempRootResponse(OpResponse):
    value: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            logs=await OperationLogs.from_reader(reader),
            value=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logs.to_writer(writer)
        self.logger.debug("to_writer", value=self.value)
        writer.write_uint64(self.value)


@dataclass
class AddTempRootRequest(OpRequest[AddTempRootResponse]):
    name: ClassVar[str] = "AddTempRoot"
    op: ClassVar[int] = 11
    response_type: ClassVar[type[OpResponse]] = AddTempRootResponse
    path: StorePath = StorePath("")

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        path = await reader.read_string(StorePath)
        cls.logger.debug("from_reader", path=path)
        return cls(path=path)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
