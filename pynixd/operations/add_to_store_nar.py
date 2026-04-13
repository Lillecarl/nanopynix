"""AddToStoreNar operation request/response types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import StorePath
from ..wire import NixReader, NixWriter, forward_framed
from .base import (
    OpRequest,
    OpResponse,
    OperationLogs,
    ValidPathInfo,
    UnkeyedValidPathInfo,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class AddToStoreNarResponse(OpResponse):
    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(logs=await OperationLogs.from_reader(reader))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer")
        self.logs.to_writer(writer)


@dataclass
class AddToStoreNarRequest(OpRequest[AddToStoreNarResponse]):
    """Prefix for AddToStoreNar (framed NAR data follows)."""

    name: ClassVar[str] = "AddToStoreNar"
    op: ClassVar[int] = 39
    response_type: ClassVar[type[OpResponse]] = AddToStoreNarResponse
    info: ValidPathInfo | None = None
    repair: int = 0
    dont_check_sigs: int = 0
    async_provider: Callable[[NixWriter], Awaitable[None]] | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        path = await reader.read_string(StorePath)
        unkeyed_info = await UnkeyedValidPathInfo.from_reader(reader)
        info = unkeyed_info.with_path(path)
        cls.logger.debug("from_reader", info=info)
        return cls(
            info=info,
            repair=await reader.read_uint64(),
            dont_check_sigs=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        if self.info is not None:
            self.info.to_writer(writer)
        writer.write_uint64(self.repair)
        writer.write_uint64(self.dont_check_sigs)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> AddToStoreNarResponse:
        if self.async_provider:
            async with store.transfer_conn() as conn:
                await self.to_writer(conn.w, conn.version)
                await self.async_provider(conn.w)
                await conn.w.drain()
                await conn.r.drain_stderr()
                return await AddToStoreNarResponse.from_reader(conn.r, conn.version)
        return await super().execute(store, client, suppress_last)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> AddToStoreNarResponse:
        """Override handle because this is a streaming operation."""
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        async with proxy.local_store.transfer_conn() as conn:
            path = await cls.forward(proxy.r, conn.w)
            resp = await AddToStoreNarResponse.from_reader(conn.r, conn.version)
            proxy.local_store.add_known_path(path)
        return resp

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> StorePath:
        """Forward request prefix and stream framed NAR data. Returns store path."""
        dst.write_uint64(39)

        path = await src.read_string(StorePath)
        unkeyed_info = await UnkeyedValidPathInfo.from_reader(src)
        info = unkeyed_info.with_path(path)

        repair = await src.read_uint64()
        dont_check_sigs = await src.read_uint64()

        cls.logger.debug(
            "forward", info=info, repair=repair, dont_check_sigs=dont_check_sigs
        )

        info.to_writer(dst)
        dst.write_uint64(repair)
        dst.write_uint64(dont_check_sigs)

        await forward_framed(src, dst)

        return info.path
