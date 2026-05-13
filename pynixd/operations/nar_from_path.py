"""NarFromPath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..store_path import StorePath
from ..types.context import ReadContext, WriteContext
from ..wire import _CHUNK_SIZE
from .base import ByteCollector, OperationLogs, OpRequest, OpResponse
from .query_path_info import QueryPathInfoRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..connection import ClientConn
    from ..store import Store
    from ..types import RequestContext as RequestContext


@dataclass
class NarFromPathResponse(OpResponse):
    """Response containing raw NAR data."""

    nar_data: bytes

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        collector = ByteCollector()
        await wire.stream_parse_nar(ctx.reader, collector, capture=False)
        obj.nar_data = collector.getvalue()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", nar_size=len(self.nar_data))
        self.logs.serialize(ctx)
        ctx.writer.write(self.nar_data)


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
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.path = await ctx.reader.read_string(StorePath)
        obj.logger.debug("deserialize", path=obj.path)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.path)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> NarFromPathResponse:
        if self.nar_size > 0:
            async with store.transfer_conn() as conn:
                w_ctx = WriteContext(writer=conn.w, version=conn.version)
                await self.serialize(w_ctx)
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
            w_ctx = WriteContext(writer=conn.w, version=conn.version)
            await NarFromPathRequest(path=path, nar_size=nar_size).serialize(w_ctx)
            await conn.w.drain()

            r_ctx = ReadContext(reader=conn.r, version=conn.version)
            logs = await OperationLogs.deserialize(r_ctx)

            await ctx.proxy.client.flush()
            logs.serialize(WriteContext(writer=ctx.proxy.w, version=ctx.version))

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
