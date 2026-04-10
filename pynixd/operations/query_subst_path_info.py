"""QuerySubstitutablePathInfo operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs, SubstPathInfo


@dataclass
class QuerySubstPathInfoResponse(OpResponse):
    found: bool = False
    info: SubstPathInfo | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        found = await reader.read_uint64() != 0
        info = None
        if found:
            info = await SubstPathInfo.from_reader(reader, version)
        return cls(logs=await OperationLogs.from_reader(reader), found=found, info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(1 if self.found else 0)
        if self.found and self.info is not None:
            await self.info.to_writer(writer, version)
        self.logs.to_writer(writer)


@dataclass
class QuerySubstPathInfoRequest(OpRequest[QuerySubstPathInfoResponse]):
    name: ClassVar[str] = "QuerySubstitutablePathInfo"
    op: ClassVar[int] = 21
    response_type: ClassVar[type[OpResponse]] = QuerySubstPathInfoResponse
    is_query: ClassVar[bool] = True
    path: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(path=await reader.read_string())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)
