"""CollectGarbage operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import (
    OpRequest,
    OpResponse,
    OperationLogs,
    RequestContext,
    Role,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class CollectGarbageResponse(OpResponse):
    paths_deleted: set[StorePath] = field(default_factory=set)
    bytes_freed: int = 0
    _obsolete: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        paths_deleted = await reader.read_string_set(StorePath)
        bytes_freed = await reader.read_uint64()
        _obsolete = await reader.read_uint64()
        return cls(
            logs=logs,
            paths_deleted=paths_deleted,
            bytes_freed=bytes_freed,
            _obsolete=_obsolete,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug(
            "to_writer", paths_deleted=self.paths_deleted, bytes_freed=self.bytes_freed
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

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        action = await reader.read_uint64()
        paths_to_delete = await reader.read_string_set(StorePath)
        ignore_liveness = await reader.read_uint64()
        max_freed = await reader.read_uint64()
        _obsolete1 = await reader.read_uint64()
        _obsolete2 = await reader.read_uint64()
        _obsolete3 = await reader.read_uint64()
        cls.logger.debug(
            "from_reader",
            action=action,
            paths_to_delete=paths_to_delete,
            ignore_liveness=ignore_liveness,
            max_freed=max_freed,
        )
        return cls(
            action=action,
            paths_to_delete=paths_to_delete,
            ignore_liveness=ignore_liveness,
            max_freed=max_freed,
            _obsolete1=_obsolete1,
            _obsolete2=_obsolete2,
            _obsolete3=_obsolete3,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_uint64(self.action)
        writer.write_string_set(self.paths_to_delete)
        writer.write_uint64(self.ignore_liveness)
        writer.write_uint64(self.max_freed)
        writer.write_uint64(self._obsolete1)
        writer.write_uint64(self._obsolete2)
        writer.write_uint64(self._obsolete3)

    @classmethod
    async def handle(cls, ctx: RequestContext) -> CollectGarbageResponse | None:
        log = structlog.get_logger(f"pynixd.operations.{cls.__name__}")
        log.debug("received_op")

        # Must always consume the request to keep protocol in sync
        request = await cls.from_reader(ctx.proxy.r, ctx.version)

        if ctx.role < Role.ADMIN:
            log.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{cls.name}' requires administrative privileges."
            )
            return None

        result = await ctx.proxy.execute(request)
        log.debug("responded_op")
        return result

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> CollectGarbageResponse:
        resp = await super().execute(store, client, suppress_last)
        store.tracker.known_paths -= resp.paths_deleted
        return resp
