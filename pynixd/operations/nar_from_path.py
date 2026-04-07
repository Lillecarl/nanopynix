"""NarFromPath operation request/response types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from .. import wire
from ..protocol import Op, op_log
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import (
    ByteCollector,
    OpRequest,
    OpResponse,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class NarFromPathResponse(OpResponse):
    """Response containing raw NAR data."""

    nar_data: bytes = b""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        collector = ByteCollector()
        await wire.stream_parse_nar(reader, collector, capture=False)
        return cls(nar_data=collector.getvalue())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write(self.nar_data)


@dataclass
class NarFromPathRequest(OpRequest[NarFromPathResponse]):
    op: ClassVar[int] = Op.NarFromPath
    response_type: ClassVar[type[OpResponse]] = NarFromPathResponse
    is_query: ClassVar[bool] = True
    path: StorePath = StorePath("")
    nar_size: int = 0
    async_callback: Callable[[bytes], Awaitable[None]] | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(path=await reader.read_string(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> NarFromPathResponse:
        if self.nar_size > 0:
            from ..wire import _CHUNK_SIZE

            async with store.transfer_conn() as conn:
                await self.to_writer(conn.w, conn.version)
                await conn.w.drain()
                await conn.r.drain_stderr()

                if self.async_callback:
                    remaining = self.nar_size
                    while remaining > 0:
                        to_read = min(remaining, _CHUNK_SIZE)
                        chunk = await conn.r.readexactly(to_read)
                        await self.async_callback(chunk)
                        remaining -= to_read
                    return NarFromPathResponse()

                data = await conn.r.readexactly(self.nar_size)
                return NarFromPathResponse(nar_data=data)

        return await super().execute(store, client, suppress_last)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> NarFromPathResponse | None:
        from .query_path_info import QueryPathInfoRequest

        structlog.contextvars.bind_contextvars(operation=cls.__name__)

        request = await cls.from_reader(proxy.r, proxy.version)
        path = request.path

        info_resp = await proxy.local_store.execute(QueryPathInfoRequest(path=path))
        if not info_resp.valid or info_resp.info is None:
            cls._log.warning("nar_not_in_local_store", path=path)
            return NarFromPathResponse(nar_data=b"")

        nar_size = info_resp.info.nar_size

        op_log("NarFromPath").debug(
            "nar_from_path_streaming",
            path=path,
            size=nar_size,
        )

        await proxy.client.flush()
        proxy.w.write_uint64(wire.STDERR_LAST)

        async with proxy.local_store.transfer_conn() as conn:
            # We explicitly write OpRequest fields here because handle is special.
            # However, we can also just create a temporary request.
            await cls(path=path).to_writer(conn.w, conn.version)
            await conn.w.drain()
            await conn.r.drain_stderr()

            if nar_size > 0:
                from ..wire import _CHUNK_SIZE

                remaining = nar_size
                while remaining > 0:
                    to_read = min(remaining, _CHUNK_SIZE)
                    chunk = await conn.r.readexactly(to_read)
                    proxy.w.write(chunk)
                    remaining -= to_read
            else:
                await wire.stream_parse_nar(conn.r, proxy.w)

        await proxy.w.drain()
        return None
