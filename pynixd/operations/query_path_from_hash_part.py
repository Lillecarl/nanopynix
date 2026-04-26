"""QueryPathFromHashPart operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import OperationLogs, OpRequest, OpResponse

QUERY_PATH_FROM_HASH_PART = """
SELECT path FROM ValidPaths WHERE path >= ? AND path < ? LIMIT 1
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..wire import NixReader, NixWriter


@dataclass
class QueryPathFromHashPartResponse(OpResponse):
    value: StorePath = field(default_factory=lambda: StorePath(""))

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
        self.value = await reader.read_string(StorePath)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_string(self.value)


@dataclass
class QueryPathFromHashPartRequest(OpRequest[QueryPathFromHashPartResponse]):
    name: ClassVar[str] = "QueryPathFromHashPart"
    op: ClassVar[int] = 29
    response_type: ClassVar[type[OpResponse]] = QueryPathFromHashPartResponse
    is_query: ClassVar[bool] = True
    path: str = ""

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.path = await reader.read_string()
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
    ) -> QueryPathFromHashPartResponse:
        if (db := store.db) is not None:
            prefix = f"/nix/store/{self.path}"
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            async with db.execute(QUERY_PATH_FROM_HASH_PART, (prefix, upper)) as cursor:
                row = await cursor.fetchone()
            if row:
                result = QueryPathFromHashPartResponse(value=StorePath(row[0]))
                store.tracker.add_known_path(result.value)
                return result

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        if resp.value:
            store.tracker.add_known_path(StorePath(resp.value))
        return resp
