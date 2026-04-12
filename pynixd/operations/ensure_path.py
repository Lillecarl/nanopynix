"""EnsurePath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class EnsurePathResponse(OpResponse):
    value: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        value = await reader.read_uint64()
        cls.logger.debug("from_reader", value=value)
        return cls(
            logs=logs,
            value=value,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)


@dataclass
class EnsurePathRequest(OpRequest[EnsurePathResponse]):
    name: ClassVar[str] = "EnsurePath"
    op: ClassVar[int] = 10
    response_type: ClassVar[type[OpResponse]] = EnsurePathResponse
    path: StorePath = StorePath("")

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(path=await reader.read_string(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
