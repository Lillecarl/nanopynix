"""AddToStore operation request/response types."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from pynixd.operations.sign_path_info import SignPathInfoRequest

from ..store_path import StorePath
from ..types import OperationLogs
from ..wire import NixReader, NixWriter, forward_framed
from .base import (
    OpRequest,
    OpResponse,
    RequestContext,
    ValidPathInfo,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..connection import ClientConn
    from ..store import Store
    from ..types.aliases import StorePathSet


@dataclass
class AddToStoreResponse(OpResponse):
    """Response: ValidPathInfo (path + UnkeyedValidPathInfo)."""

    info: ValidPathInfo | None = None

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
        obj.info = await ValidPathInfo.from_reader(reader)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", info=self.info)
        self.logs.to_writer(writer)
        if self.info is not None:
            self.info.to_writer(writer)


@dataclass(kw_only=True)
class AddToStoreRequest(OpRequest[AddToStoreResponse]):
    """Prefix for AddToStore (framed NAR data follows)."""

    name: ClassVar[str] = "AddToStore"
    op: ClassVar[int] = 7
    response_type: ClassVar[type[OpResponse]] = AddToStoreResponse
    path_name: str
    cam: str  # ContentAddressMethodWithAlgo
    references: StorePathSet
    repair: int
    async_provider: Callable[[NixWriter], Awaitable[None]] | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.path_name = await reader.read_string()
        obj.cam = await reader.read_string()
        obj.references = await reader.read_string_set(StorePath)
        obj.repair = await reader.read_uint64()
        obj.logger.debug(
            "from_reader",
            path_name=obj.path_name,
            cam=obj.cam,
            references=obj.references,
            repair=obj.repair,
        )
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path_name)
        writer.write_string(self.cam)
        writer.write_string_set(self.references)
        writer.write_uint64(self.repair)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> AddToStoreResponse:
        if self.async_provider:
            async with store.transfer_conn() as conn:
                await self.to_writer(conn.w, conn.version)
                await conn.w.drain()

                async def write_payload():
                    assert self.async_provider is not None
                    await self.async_provider(conn.w)
                    await conn.w.drain()

                async with asyncio.TaskGroup() as tg:
                    resp_task = tg.create_task(
                        AddToStoreResponse.from_reader(conn.r, conn.version, client),
                    )
                    tg.create_task(write_payload())

                return await resp_task

        return await super().execute(store, client, suppress_last)

    async def handle(self, ctx: RequestContext) -> AddToStoreResponse:
        """Override handle because this is a streaming operation."""
        structlog.contextvars.bind_contextvars(operation=type(self).__name__)
        async with ctx.proxy.local_store.transfer_conn() as conn:
            await self.forward(ctx.proxy.r, conn.w)
            await conn.w.drain()

            # We don't concurrently read stderr here because we are forwarding
            # raw bytes from the client. If the daemon errors, the client's
            # forward will eventually fail or timeout.
            # However, for consistency with execute(), we should ideally read
            # logs concurrently here too. But forward() is synchronous-ish
            # (awaits reads/writes).

            resp = await AddToStoreResponse.from_reader(conn.r, conn.version)
            if resp.info is not None:
                resp.info = (
                    await ctx.proxy.local_store.execute(
                        SignPathInfoRequest(info=resp.info),
                    )
                ).info
                if resp.info is not None:
                    ctx.proxy.local_store.tracker.add_known_path(resp.info.path)
                    ctx.proxy.local_store.add_path_info(resp.info)
            return resp

    async def forward(self, src: NixReader, dst: NixWriter) -> None:
        """Forward request prefix and stream framed NAR data from src to dst."""
        self.logger = self.logger.bind(identifier=src.identifier)
        dst.write_uint64(7)

        path_name = await src.read_string()
        cam = await src.read_string()
        references = await src.read_string_set(StorePath)
        repair = await src.read_uint64()
        self.logger.debug(
            "forward",
            path_name=path_name,
            cam=cam,
            references=references,
            repair=repair,
        )

        dst.write_string(path_name)
        dst.write_string(cam)
        dst.write_string_set(references)
        dst.write_uint64(repair)

        await forward_framed(src, dst)