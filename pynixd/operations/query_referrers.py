"""QueryReferrers operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OpRequest, OpResponse

from ..types.aliases import StorePathSet
if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class QueryReferrersResponse(OpResponse):
    paths: StorePathSet = field(default_factory=set)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(reader)
        self.paths = await reader.read_string_set(StorePath)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", paths=self.paths)
        self.logs.to_writer(writer)
        writer.write_string_set(self.paths)


@dataclass
class QueryReferrersRequest(OpRequest[QueryReferrersResponse]):
    name: ClassVar[str] = "QueryReferrers"
    op: ClassVar[int] = 6
    response_type: ClassVar[type[OpResponse]] = QueryReferrersResponse
    is_query: ClassVar[bool] = True
    path: StorePath = field(default_factory=lambda: StorePath(""))

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.path = await reader.read_string(StorePath)
        self.logger.debug("from_reader", path=self.path)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)
