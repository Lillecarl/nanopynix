"""QuerySubstitutablePathInfo operation request/response types.

Deprecated in favor of QuerySubstitutablePaths (op 32).
Kept for backward compatibility with older daemon protocol versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .base import OpRequest, OpResponse, OperationLogs, SubstitutablePathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class QuerySubstitutablePathInfoResponse(OpResponse):
    found: bool = False
    info: SubstitutablePathInfo | None = None

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
        obj.found = await reader.read_uint64() != 0
        obj.info = None
        if obj.found:
            obj.info = await SubstitutablePathInfo.from_reader(reader, version)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", found=self.found)
        self.logs.to_writer(writer)
        writer.write_uint64(1 if self.found else 0)
        if self.found and self.info is not None:
            await self.info.to_writer(writer, version)


@dataclass
class QuerySubstitutablePathInfoRequest(OpRequest[QuerySubstitutablePathInfoResponse]):
    name: ClassVar[str] = "QuerySubstitutablePathInfo"
    op: ClassVar[int] = 21
    response_type: ClassVar[type[OpResponse]] = QuerySubstitutablePathInfoResponse
    is_query: ClassVar[bool] = True
    path: str = ""

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
        obj.path = await reader.read_string()
        obj.logger.debug("from_reader", path=obj.path)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)
