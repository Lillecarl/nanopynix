"""CollectGarbage operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import StorePath
from ..types import OperationLogs
from .base import (
    OpRequest,
    OpResponse,
    RequestContext,
    Role,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.aliases import StorePathSet
    from ..wire import NixReader, NixWriter


@dataclass
class CollectGarbageResponse(OpResponse):
    paths_deleted: StorePathSet
    bytes_freed: int
    _obsolete: int

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
        await obj.logs.from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        obj.paths_deleted = await reader.read_string_set(StorePath)
        obj.bytes_freed = await reader.read_uint64()
        obj._obsolete = await reader.read_uint64()
        return obj

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


@dataclass(kw_only=True)
class CollectGarbageRequest(OpRequest[CollectGarbageResponse]):
    name: ClassVar[str] = "CollectGarbage"
    op: ClassVar[int] = 20
    response_type: ClassVar[type[OpResponse]] = CollectGarbageResponse
    action: int
    paths_to_delete: StorePathSet
    ignore_liveness: int
    max_freed: int
    _obsolete1: int
    _obsolete2: int
    _obsolete3: int

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
        obj.action = await reader.read_uint64()
        obj.paths_to_delete = await reader.read_string_set(StorePath)
        obj.ignore_liveness = await reader.read_uint64()
        obj.max_freed = await reader.read_uint64()
        obj._obsolete1 = await reader.read_uint64()
        obj._obsolete2 = await reader.read_uint64()
        obj._obsolete3 = await reader.read_uint64()
        obj.logger.debug(
            "from_reader",
            action=obj.action,
            paths_to_delete=obj.paths_to_delete,
            ignore_liveness=obj.ignore_liveness,
            max_freed=obj.max_freed,
        )
        return obj

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
        self = await self.from_reader(ctx.proxy.r, ctx.version)

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