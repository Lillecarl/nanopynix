"""AddToStoreNar operation request/response types."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..stderr import OperationLogs
from ..store_path import StorePath
from ..wire import NixReader, NixWriter, forward_framed
from .base import OpRequest, OpResponse, UnkeyedValidPathInfo, ValidPathInfo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..connection import ClientConn
    from ..store import Store
    from ..types import RequestContext as RequestContext
from ..types.context import ReadContext, WriteContext


@dataclass
class AddToStoreNarResponse(OpResponse):
    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        ctx = ReadContext(reader=reader, version=version, client=client, buffer_logs=buffer_logs)
        return await cls.deserialize(ctx)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        ctx = WriteContext(writer=writer, version=version)
        await self.serialize(ctx)

    # ── New-style API (ReadContext / WriteContext) ──────────────

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logs.serialize(ctx)


@dataclass(kw_only=True)
class AddToStoreNarRequest(OpRequest[AddToStoreNarResponse]):
    """Prefix for AddToStoreNar (framed NAR data follows)."""

    name: ClassVar[str] = "AddToStoreNar"
    op: ClassVar[int] = 39
    response_type: ClassVar[type[OpResponse]] = AddToStoreNarResponse
    info: ValidPathInfo | None = None
    repair: int
    dont_check_sigs: int
    async_provider: Callable[[NixWriter], Awaitable[None]] | None = None

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
    ) -> Self:
        ctx = ReadContext(reader=reader, version=version)
        return await cls.deserialize(ctx)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        ctx = WriteContext(writer=writer, version=version)
        await self.serialize(ctx)

    # ── New-style API (ReadContext / WriteContext) ──────────────

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        path = await ctx.reader.read_string(StorePath)
        unkeyed_info = await UnkeyedValidPathInfo.deserialize(ctx)
        obj.info = unkeyed_info.with_path(path)
        obj.repair = await ctx.reader.read_uint64()
        obj.dont_check_sigs = await ctx.reader.read_uint64()
        obj.logger.debug("deserialize", info=obj.info)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        if self.info is not None:
            self.info.serialize(ctx)
        ctx.writer.write_uint64(self.repair)
        ctx.writer.write_uint64(self.dont_check_sigs)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> AddToStoreNarResponse:
        if provider := self.async_provider:
            async with store.transfer_conn() as conn:
                await self.to_writer(conn.w, conn.version)
                await conn.w.drain()

                async def write_payload():
                    await provider(conn.w)
                    await conn.w.drain()

                async with asyncio.TaskGroup() as tg:
                    resp_task = tg.create_task(
                        AddToStoreNarResponse.from_reader(conn.r, conn.version, client),
                    )
                    tg.create_task(write_payload())

                return await resp_task

        return await super().execute(store, client, suppress_last)

    async def handle(self, ctx: RequestContext) -> AddToStoreNarResponse:
        """Override handle because this is a streaming operation."""
        structlog.contextvars.bind_contextvars(operation=type(self).__name__)
        async with ctx.proxy.local_store.transfer_conn() as conn:
            path = await self.forward(ctx.proxy.r, conn.w)
            r_ctx = ReadContext(reader=conn.r, version=conn.version)
            resp = await AddToStoreNarResponse.deserialize(r_ctx)
            ctx.proxy.local_store.tracker.add_known_path(path)
        return resp

    async def forward(self, src: NixReader, dst: NixWriter) -> StorePath:
        """Forward request prefix and stream framed NAR data. Returns store path."""
        self.logger = self.logger.bind(identifier=src.identifier)
        dst.write_uint64(39)

        path = await src.read_string(StorePath)
        unkeyed_info = await UnkeyedValidPathInfo.deserialize(ReadContext(reader=src, version=1))
        info = unkeyed_info.with_path(path)

        repair = await src.read_uint64()
        dont_check_sigs = await src.read_uint64()

        self.logger.debug(
            "forward",
            info=info,
            repair=repair,
            dont_check_sigs=dont_check_sigs,
        )

        info.serialize(WriteContext(writer=dst, version=1))
        dst.write_uint64(repair)
        dst.write_uint64(dont_check_sigs)

        await forward_framed(src, dst)

        return info.path
