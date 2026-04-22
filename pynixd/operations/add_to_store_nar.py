"""AddToStoreNar operation request/response types."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..exceptions import BackendError
from ..stderr import StderrError, read_stream
from ..store_path import StorePath
from ..wire import NixReader, NixWriter, forward_framed
from .base import (
    OperationLogs,
    OpRequest,
    OpResponse,
    RequestContext,
    UnkeyedValidPathInfo,
    ValidPathInfo,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class AddToStoreNarResponse(OpResponse):
    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(reader, client=client, buffer=buffer_logs)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
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

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        path = await reader.read_string(StorePath)
        unkeyed_info = await UnkeyedValidPathInfo().from_reader(reader)
        self.info = unkeyed_info.with_path(path)
        self.repair = await reader.read_uint64()
        self.dont_check_sigs = await reader.read_uint64()
        self.logger.debug("from_reader", info=self.info)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
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
                await conn.w.drain()

                logs = OperationLogs()
                error = None

                async def read_stderr():
                    nonlocal error
                    try:
                        async for msg in read_stream(conn.r):
                            logs.add(msg)
                            if isinstance(msg, StderrError):
                                error = BackendError(f"Backend error: {msg.msg}")
                                return
                            if client:
                                client.queue.put_nowait(msg)
                    except Exception as e:
                        error = e

                stderr_task = asyncio.create_task(read_stderr())

                try:
                    await self.async_provider(conn.w)
                    await conn.w.drain()
                except Exception as e:
                    if not error:
                        error = e

                await stderr_task

                if error:
                    raise error

                return AddToStoreNarResponse(logs=logs)

        return await super().execute(store, client, suppress_last)

    async def handle(self, ctx: RequestContext) -> AddToStoreNarResponse:
        """Override handle because this is a streaming operation."""
        structlog.contextvars.bind_contextvars(operation=type(self).__name__)
        async with ctx.proxy.local_store.transfer_conn() as conn:
            path = await self.forward(ctx.proxy.r, conn.w)
            resp = await AddToStoreNarResponse().from_reader(conn.r, conn.version)
            ctx.proxy.local_store.tracker.add_known_path(path)
        return resp

    async def forward(self, src: NixReader, dst: NixWriter) -> StorePath:
        """Forward request prefix and stream framed NAR data. Returns store path."""
        self.logger = self.logger.bind(identifier=src.identifier)
        dst.write_uint64(39)

        path = await src.read_string(StorePath)
        unkeyed_info = await UnkeyedValidPathInfo().from_reader(src)
        info = unkeyed_info.with_path(path)

        repair = await src.read_uint64()
        dont_check_sigs = await src.read_uint64()

        self.logger.debug(
            "forward", info=info, repair=repair, dont_check_sigs=dont_check_sigs
        )

        info.to_writer(dst)
        dst.write_uint64(repair)
        dst.write_uint64(dont_check_sigs)

        await forward_framed(src, dst)

        return info.path
