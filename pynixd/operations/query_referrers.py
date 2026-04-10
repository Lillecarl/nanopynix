"""QueryReferrers operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs


@dataclass
class QueryReferrersResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            logs=await OperationLogs.from_reader(reader),
            paths=await reader.read_string_set(StorePath),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logs.to_writer(writer)
        writer.write_string_set(self.paths)


@dataclass
class QueryReferrersRequest(OpRequest[QueryReferrersResponse]):
    name: ClassVar[str] = "QueryReferrers"
    op: ClassVar[int] = 6
    response_type: ClassVar[type[OpResponse]] = QueryReferrersResponse
    is_query: ClassVar[bool] = True
    path: StorePath = StorePath("")

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(path=await reader.read_string(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
