"""QueryDerivationOutputMap operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OperationLogs, OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..wire import NixReader, NixWriter


@dataclass
class QueryDerivationOutputMapResponse(OpResponse):
    items: dict[str, StorePath | None] = field(default_factory=dict)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        n = await reader.read_uint64()
        self.items: dict[str, StorePath | None] = {}
        for _ in range(n):
            k = await reader.read_string()
            raw = await reader.read_string()
            self.items[k] = StorePath(raw) if raw else None
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", item_count=len(self.items))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            writer.write_string(k)
            writer.write_string(v if v is not None else StorePath(""))


@dataclass
class QueryDerivationOutputMapRequest(OpRequest[QueryDerivationOutputMapResponse]):
    name: ClassVar[str] = "QueryDerivationOutputMap"
    op: ClassVar[int] = 41
    response_type: ClassVar[type[OpResponse]] = QueryDerivationOutputMapResponse
    is_query: ClassVar[bool] = True
    path: StorePath = StorePath("")

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

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryDerivationOutputMapResponse:
        resp = await store.call(self, client=client, suppress_last=suppress_last)
        resolved = {v for v in resp.items.values() if v is not None}
        if resolved:
            store.tracker.add_known_paths(resolved)
        return resp
