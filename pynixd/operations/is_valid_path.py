"""IsValidPath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse, OperationLogs

IS_VALID_PATH = "SELECT 1 FROM ValidPaths WHERE path = ? LIMIT 1"

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class IsValidPathResponse(OpResponse):
    valid: bool = False

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(reader, client=client, buffer=buffer_logs)
        self.valid = await reader.read_uint64() != 0
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", valid=self.valid)
        self.logs.to_writer(writer)
        writer.write_uint64(1 if self.valid else 0)


@dataclass
class IsValidPathRequest(OpRequest[IsValidPathResponse]):
    name: ClassVar[str] = "IsValidPath"
    op: ClassVar[int] = 1
    response_type: ClassVar[type[OpResponse]] = IsValidPathResponse
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
    ) -> IsValidPathResponse:
        if store.tracker.has_path(self.path):
            return IsValidPathResponse(valid=True)

        if (db := store.native_db) is not None:
            async with db.execute(IS_VALID_PATH, (self.path,)) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                store.tracker.add_known_path(self.path)
                return IsValidPathResponse(valid=True)

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        if resp.valid:
            store.tracker.add_known_path(self.path)
        return resp
