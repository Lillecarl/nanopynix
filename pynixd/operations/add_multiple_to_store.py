"""AddMultipleToStore operation request/response types."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs, StderrNext
from ..wire import FramedReader, FramedWriter, NixReader, NixWriter
from .base import OpRequest, OpResponse, ValidPathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..types import RequestContext as RequestContext
    from ..types.context import ReadContext, WriteContext


@dataclass
class AddMultipleToStoreResponse(OpResponse):
    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(reader, client=client, buffer=buffer_logs)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer")
        self.logs.to_writer(writer)

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
class AddMultipleToStoreRequest(OpRequest[AddMultipleToStoreResponse]):
    """Prefix for AddMultipleToStore (framed data follows)."""

    name: ClassVar[str] = "AddMultipleToStore"
    op: ClassVar[int] = 44
    response_type: ClassVar[type[OpResponse]] = AddMultipleToStoreResponse
    repair: int
    dont_check_sigs: int

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.repair = await reader.read_uint64()
        obj.dont_check_sigs = await reader.read_uint64()
        obj.logger.debug(
            "from_reader",
            repair=obj.repair,
            dont_check_sigs=obj.dont_check_sigs,
        )
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_uint64(self.repair)
        writer.write_uint64(self.dont_check_sigs)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.repair = await ctx.reader.read_uint64()
        obj.dont_check_sigs = await ctx.reader.read_uint64()
        obj.logger.debug(
            "deserialize",
            repair=obj.repair,
            dont_check_sigs=obj.dont_check_sigs,
        )
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_uint64(self.repair)
        ctx.writer.write_uint64(self.dont_check_sigs)

    async def handle(self, ctx: RequestContext) -> AddMultipleToStoreResponse:
        """Override handle because this is a streaming operation."""
        self = await self.from_reader(ctx.proxy.r, ctx.version)
        async with ctx.proxy.local_store.transfer_conn() as conn:
            # Re-write the request prefix to the backend
            await self.to_writer(conn.w, ctx.version)
            await conn.w.drain()

            # We must run forward_stream and the response reader concurrently
            # because the backend might send logs while we are still sending data.
            # If we don't read the logs, the backend's output buffer fills and it blocks.
            async with asyncio.TaskGroup() as tg:
                resp_task = tg.create_task(
                    AddMultipleToStoreResponse.from_reader(conn.r, conn.version),
                )

                infos = await self.forward_stream(ctx.proxy.r, conn.w)
                resp = await resp_task

            resp.logs.messages.append(
                StderrNext("pynixd: AddMultipleToStore forwarding complete"),
            )

            ctx.proxy.local_store.add_path_infos(infos)
            ctx.proxy.local_store.tracker.add_known_paths({i.path for i in infos})
            return resp

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
            info = await ValidPathInfo.from_reader(fsrc)
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
