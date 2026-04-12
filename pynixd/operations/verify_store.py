"""VerifyStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class VerifyStoreResponse(OpResponse):
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
class VerifyStoreRequest(OpRequest[VerifyStoreResponse]):
    name: ClassVar[str] = "VerifyStore"
    op: ClassVar[int] = 35
    response_type: ClassVar[type[OpResponse]] = VerifyStoreResponse
    check_contents: int = 0
    repair: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        check_contents = await reader.read_uint64()
        repair = await reader.read_uint64()
        cls.logger.debug("from_reader", check_contents=check_contents, repair=repair)
        return cls(
            check_contents=check_contents,
            repair=repair,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_uint64(self.check_contents)
        writer.write_uint64(self.repair)
