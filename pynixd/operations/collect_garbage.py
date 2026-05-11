"""CollectGarbage operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from ..types import GCAction
from ..types.context import ReadContext
from .base import OpRequest, OpResponse, Role

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types import RequestContext as RequestContext
    from ..types.aliases import StorePathSet
    from ..types.context import WriteContext
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
        version: int,  # noqa: ARG003
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

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.paths_deleted = await ctx.reader.read_string_set(StorePath)
        obj.bytes_freed = await ctx.reader.read_uint64()
        obj._obsolete = await ctx.reader.read_uint64()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug(
            "serialize",
            paths_deleted=self.paths_deleted,
            bytes_freed=self.bytes_freed,
        )
        self.logs.serialize(ctx)
        ctx.writer.write_string_set(self.paths_deleted)
        ctx.writer.write_uint64(self.bytes_freed)
        ctx.writer.write_uint64(self._obsolete)


@dataclass(kw_only=True)
class CollectGarbageRequest(OpRequest[CollectGarbageResponse]):
    name: ClassVar[str] = "CollectGarbage"
    op: ClassVar[int] = 20
    response_type: ClassVar[type[OpResponse]] = CollectGarbageResponse
    action: GCAction
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
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,  # noqa: ARG003
        buffer_logs: bool = True,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.action = GCAction(await reader.read_uint64())
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

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.action = GCAction(await ctx.reader.read_uint64())
        obj.paths_to_delete = await ctx.reader.read_string_set(StorePath)
        obj.ignore_liveness = await ctx.reader.read_uint64()
        obj.max_freed = await ctx.reader.read_uint64()
        obj._obsolete1 = await ctx.reader.read_uint64()
        obj._obsolete2 = await ctx.reader.read_uint64()
        obj._obsolete3 = await ctx.reader.read_uint64()
        obj.logger.debug(
            "deserialize",
            action=obj.action,
            paths_to_delete=obj.paths_to_delete,
            ignore_liveness=obj.ignore_liveness,
            max_freed=obj.max_freed,
        )
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_uint64(self.action)
        ctx.writer.write_string_set(self.paths_to_delete)
        ctx.writer.write_uint64(self.ignore_liveness)
        ctx.writer.write_uint64(self.max_freed)
        ctx.writer.write_uint64(self._obsolete1)
        ctx.writer.write_uint64(self._obsolete2)
        ctx.writer.write_uint64(self._obsolete3)

    async def handle(self, ctx: RequestContext) -> CollectGarbageResponse | None:
        self.logger.debug("received_op")

        # Must always consume the request to keep protocol in sync
        r_ctx = ReadContext(reader=ctx.proxy.r, version=ctx.version)
        self = await self.deserialize(r_ctx)

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
