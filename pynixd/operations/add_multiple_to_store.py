"""AddMultipleToStore operation request/response types."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import StderrNext
from ..wire import FramedReader, FramedWriter, NixReader, NixWriter
from .base import OpRequest, OpResponse, RequestContext, ValidPathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn


@dataclass
class AddMultipleToStoreResponse(OpResponse):
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
class AddMultipleToStoreRequest(OpRequest[AddMultipleToStoreResponse]):
    """Prefix for AddMultipleToStore (framed data follows)."""

    name: ClassVar[str] = "AddMultipleToStore"
    op: ClassVar[int] = 44
    response_type: ClassVar[type[OpResponse]] = AddMultipleToStoreResponse
    repair: int = 0
    dont_check_sigs: int = 0

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.repair = await reader.read_uint64()
        self.dont_check_sigs = await reader.read_uint64()
        self.logger.debug(
            "from_reader",
            repair=self.repair,
            dont_check_sigs=self.dont_check_sigs,
        )
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_uint64(self.repair)
        writer.write_uint64(self.dont_check_sigs)

    async def handle(self, ctx: RequestContext) -> AddMultipleToStoreResponse:
        """Override handle because this is a streaming operation."""
        await self.from_reader(ctx.proxy.r, ctx.version)
        async with ctx.proxy.local_store.transfer_conn() as conn:
            # Re-write the request prefix to the backend
            await self.to_writer(conn.w, conn.version)
            await conn.w.drain()

            # We must run forward_stream and the response reader concurrently
            # because the backend might send logs while we are still sending data.
            # If we don't read the logs, the backend's output buffer fills and it blocks.

            # Use a task to read the response (including logs)
            resp_task = asyncio.create_task(
                AddMultipleToStoreResponse().from_reader(conn.r, conn.version),
            )

            try:
                infos = await self.forward_stream(ctx.proxy.r, conn.w)
                resp = await resp_task
                resp.logs.messages.append(
                    StderrNext("pynixd: AddMultipleToStore forwarding complete"),
                )

                ctx.proxy.local_store.add_path_infos(infos)
                ctx.proxy.local_store.tracker.add_known_paths({i.path for i in infos})
                return resp
            finally:
                if not resp_task.done():
                    resp_task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await resp_task

    async def forward_stream(
        self,
        src: NixReader,
        dst: NixWriter,
    ) -> set[ValidPathInfo]:
        """Forward AddMultipleToStore payload verbatim, snooping ValidPathInfos."""
        self.logger = self.logger.bind(identifier=src.identifier)
        fsrc = FramedReader(src)
        fdst = FramedWriter(dst)

        expected = await fsrc.read_uint64()
        self.logger.info("forward", expected_paths=expected)
        fdst.write_uint64(expected)

        infos: set[ValidPathInfo] = set()
        for _ in range(expected):
            info = await ValidPathInfo().from_reader(fsrc)
            infos.add(info)
            self.logger.info(
                "forward_path_start",
                path=info.path,
                nar_size=info.nar_size,
            )
            fdst.write(info.to_bytes())
            sent_bytes = 0
            while sent_bytes < info.nar_size:
                read = min(info.nar_size - sent_bytes, 1024 * 1024)
                data = await fsrc.readexactly(read)
                fdst.write(data)
                sent_bytes += len(data)
            self.logger.info("forward_path_sent", sent_bytes=sent_bytes, path=info.path)

        await fdst.finalize()
        self.logger.debug("forward_finalized")
        await fsrc.ensure_eof()
        self.logger.debug("forward_eof_reached")
        return infos
