"""CollectGarbage operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from .base import (
    OperationLogs,
    OpRequest,
    OpResponse,
    RequestContext,
    Role,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..wire import NixReader, NixWriter


@dataclass
class CollectGarbageResponse(OpResponse):
    paths_deleted: set[StorePath] = field(default_factory=set)
    bytes_freed: int = 0
    _obsolete: int = 0

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
        self.paths_deleted = await reader.read_string_set(StorePath)
        self.bytes_freed = await reader.read_uint64()
        self._obsolete = await reader.read_uint64()
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug(
            "to_writer",
            paths_deleted=self.paths_deleted,
            bytes_freed=self.bytes_freed,
        )
        self.logs.to_writer(writer)
        writer.write_string_set(self.paths_deleted)
        writer.write_uint64(self.bytes_freed)
        writer.write_uint64(self._obsolete)


@dataclass
class CollectGarbageRequest(OpRequest[CollectGarbageResponse]):
    name: ClassVar[str] = "CollectGarbage"
    op: ClassVar[int] = 20
    response_type: ClassVar[type[OpResponse]] = CollectGarbageResponse
    action: int = 0
    paths_to_delete: set[StorePath] = field(default_factory=set)
    ignore_liveness: int = 0
    max_freed: int = 0
    _obsolete1: int = 0
    _obsolete2: int = 0
    _obsolete3: int = 0

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.action = await reader.read_uint64()
        self.paths_to_delete = await reader.read_string_set(StorePath)
        self.ignore_liveness = await reader.read_uint64()
        self.max_freed = await reader.read_uint64()
        self._obsolete1 = await reader.read_uint64()
        self._obsolete2 = await reader.read_uint64()
        self._obsolete3 = await reader.read_uint64()
        self.logger.debug(
            "from_reader",
            action=self.action,
            paths_to_delete=self.paths_to_delete,
            ignore_liveness=self.ignore_liveness,
            max_freed=self.max_freed,
        )
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_uint64(self.action)
        writer.write_string_set(self.paths_to_delete)
        writer.write_uint64(self.ignore_liveness)
        writer.write_uint64(self.max_freed)
        writer.write_uint64(self._obsolete1)
        writer.write_uint64(self._obsolete2)
        writer.write_uint64(self._obsolete3)

    async def handle(self, ctx: RequestContext) -> CollectGarbageResponse | None:
        self.logger.debug("received_op")

        # Must always consume the request to keep protocol in sync
        await self.from_reader(ctx.proxy.r, ctx.version)

        if ctx.role < Role.ADMIN:
            self.logger.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{self.name}' requires administrative privileges.",
            )
            return None

        result = await ctx.proxy.execute(self)
        self.logger.debug("responded_op")
        return result

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> CollectGarbageResponse:
        resp = await super().execute(store, client, suppress_last)
        store.tracker.remove_known_paths(resp.paths_deleted)
        return resp
