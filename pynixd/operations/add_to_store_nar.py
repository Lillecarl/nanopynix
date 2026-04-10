"""AddToStoreNar operation request/response types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import StorePath
from ..stderr import read_stream
from ..wire import NixReader, NixWriter, forward_framed
from .base import OpRequest, OpResponse, OperationLogs, PathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class AddToStoreNarResponse(OpResponse):
    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls()

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logs.to_writer(writer)


@dataclass
class AddToStoreNarRequest(OpRequest[AddToStoreNarResponse]):
    """Prefix for AddToStoreNar (framed NAR data follows)."""

    name: ClassVar[str] = "AddToStoreNar"
    op: ClassVar[int] = 39
    response_type: ClassVar[type[OpResponse]] = AddToStoreNarResponse
    info: PathInfo = field(default_factory=PathInfo)
    repair: int = 0
    dont_check_sigs: int = 0
    async_provider: Callable[[NixWriter], Awaitable[None]] | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        info = PathInfo(
            path=await reader.read_string(StorePath),
            deriver=await reader.read_string(StorePath),
            nar_hash=await reader.read_string(),
            references=await reader.read_string_set(StorePath),
            registration_time=await reader.read_uint64(),
            nar_size=await reader.read_uint64(),
            ultimate=await reader.read_uint64(),
            sigs=await reader.read_string_set(),
            ca=await reader.read_string(),
        )
        return cls(
            info=info,
            repair=await reader.read_uint64(),
            dont_check_sigs=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.info.path)
        writer.write_string(self.info.deriver)
        writer.write_string(self.info.nar_hash)
        writer.write_string_set(self.info.references)
        writer.write_uint64(self.info.registration_time)
        writer.write_uint64(self.info.nar_size)
        writer.write_uint64(self.info.ultimate)
        writer.write_string_set(self.info.sigs)
        writer.write_string(self.info.ca)
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
            await conn.w.drain()

            logs = OperationLogs()
            async for msg in read_stream(conn.r):
                logs.add(msg)

            response = await AddToStoreNarResponse.from_reader(conn.r, conn.version)
            response.logs = logs
            proxy.local_store.add_known_path(path)
            return response

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> StorePath:
        """Forward request prefix and stream framed NAR data. Returns store path."""
        dst.write_uint64(39)

        path = await src.read_string(StorePath)
        deriver = await src.read_string(StorePath)
        nar_hash = await src.read_string()
        refs = await src.read_string_set(StorePath)
        reg_time = await src.read_uint64()
        nar_size = await src.read_uint64()
        ultimate = await src.read_uint64()
        sigs = await src.read_string_set()
        ca = await src.read_string()
        repair = await src.read_uint64()
        dont_check_sigs = await src.read_uint64()

        dst.write_string(path)
        dst.write_string(deriver)
        dst.write_string(nar_hash)
        dst.write_string_set(refs)
        dst.write_uint64(reg_time)
        dst.write_uint64(nar_size)
        dst.write_uint64(ultimate)
        dst.write_string_set(sigs)
        dst.write_string(ca)
        dst.write_uint64(repair)
        dst.write_uint64(dont_check_sigs)

        await forward_framed(src, dst)

        return path
