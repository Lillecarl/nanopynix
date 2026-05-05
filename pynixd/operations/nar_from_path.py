"""NarFromPath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..store_path import StorePath
from ..wire import _CHUNK_SIZE, NixReader, NixWriter
from .base import (
    ByteCollector,
    OperationLogs,
    OpRequest,
    OpResponse,
    RequestContext,
)
from .query_path_info import QueryPathInfoRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..connection import ClientConn
    from ..store import Store


@dataclass
class NarFromPathResponse(OpResponse):
    """Response containing raw NAR data."""

    nar_data: bytes

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
        await obj.logs.from_reader(reader, client=client, buffer=buffer_logs)
        collector = ByteCollector()
        await wire.stream_parse_nar(reader, collector, capture=False)
        obj.nar_data = collector.getvalue()
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", nar_size=len(self.nar_data))
        self.logs.to_writer(writer)
        writer.write(self.nar_data)


@dataclass(kw_only=True)
class NarFromPathRequest(OpRequest[NarFromPathResponse]):
    name: ClassVar[str] = "NarFromPath"
    op: ClassVar[int] = 38
    response_type: ClassVar[type[OpResponse]] = NarFromPathResponse
    is_query: ClassVar[bool] = True
    path: StorePath
    nar_size: int
    async_callback: Callable[[bytes], Awaitable[None]] | None = None

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
        obj.path = await reader.read_string(StorePath)
        obj.logger.debug("from_reader", path=obj.path)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> NarFromPathResponse:
        if self.nar_size > 0:
            async with store.transfer_conn() as conn:
                await self.to_writer(conn.w, conn.version)
                await conn.w.drain()

                # Drain logs from backend before reading payload
                await conn.r.drain_stderr()

                if self.async_callback:
                    remaining = self.nar_size
                    while remaining > 0:
                        to_read = min(remaining, _CHUNK_SIZE)
                        chunk = await conn.r.readexactly(to_read)
                        await self.async_callback(chunk)
                        remaining -= to_read
                    return NarFromPathResponse(nar_data=b"")

                data = await conn.r.readexactly(self.nar_size)
                return NarFromPathResponse(nar_data=data)

        return await super().execute(store, client, suppress_last)

    async def handle(self, ctx: RequestContext) -> NarFromPathResponse | None:
        """Override handle because this is a streaming operation."""
        self.logger = self.logger.bind(identifier=ctx.proxy.r.identifier)
        self.path = await ctx.proxy.r.read_string(StorePath)
        path = self.path

        info_resp = await ctx.proxy.local_store.execute(QueryPathInfoRequest(path=path))
        if not info_resp.valid or info_resp.info is None:
            self.logger.warning("nar_not_in_local_store", path=path)
            self.logger.debug("responded_op")
            return NarFromPathResponse(nar_data=b"")

        nar_size = info_resp.info.nar_size

        self.logger.debug(
            "nar_from_path_streaming",
            path=path,
            size=nar_size,
        )

        async with ctx.proxy.local_store.transfer_conn() as conn:
            await NarFromPathRequest(path=path, nar_size=nar_size).to_writer(conn.w, conn.version)
            await conn.w.drain()

            logs = await OperationLogs.from_reader(conn.r)

            await ctx.proxy.client.flush()
            logs.to_writer(ctx.proxy.w)

            if nar_size > 0:
                remaining = nar_size
                while remaining > 0:
                    to_read = min(remaining, _CHUNK_SIZE)
                    chunk = await conn.r.readexactly(to_read)
                    ctx.proxy.w.write(chunk)
                    remaining -= to_read
            else:
                await wire.stream_parse_nar(conn.r, ctx.proxy.w)

        await ctx.proxy.w.drain()
        self.logger.debug("responded_op")
        return None