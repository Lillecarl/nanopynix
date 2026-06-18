"""Handler for AddMultipleToStore (op 44)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

import structlog

from ..serde.add_multiple_to_store import (
    AddMultipleToStoreRequest,
    AddMultipleToStoreResponse,
)
from ..types.context import ReadContext, WriteContext
from ..types.path_info import ValidPathInfo as OldValidPathInfo
from ..wire import FramedReader, FramedWriter, NixReader, NixWriter
from ._base import Handler

if TYPE_CHECKING:
    from ..types import RequestContext

logger = structlog.get_logger(__name__)


class AddMultipleToStoreHandler(Handler):
    """Server handler for AddMultipleToStore — streaming with framed NAR forwarding."""

    op: ClassVar[int] = 44

    async def handle(self, ctx: RequestContext) -> AddMultipleToStoreResponse | None:
        async with ctx.proxy.local_store.transfer_conn() as conn:
            # 1. Read request header from client (serde)
            req = await AddMultipleToStoreRequest.from_reader(
                ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
            )

            # 2. Write request header to daemon
            await req.to_writer(WriteContext.from_conn(conn))
            await conn.w.drain()

            # 3. Concurrently: forward payload + read daemon response
            async def _read_response() -> AddMultipleToStoreResponse:
                return await AddMultipleToStoreResponse.from_reader(
                    ReadContext.from_conn(conn),
                )

            async with asyncio.TaskGroup() as tg:
                resp_task = tg.create_task(_read_response())
                infos = await self._forward_stream(ctx.proxy.r, conn.w)
                resp = await resp_task

            # Update path info cache
            ctx.proxy.local_store.add_path_infos(infos)

            return resp

    async def _forward_stream(
        self,
        src: NixReader,
        dst: NixWriter,
    ) -> set[OldValidPathInfo]:
        """Forward AddMultipleToStore payload, snooping ValidPathInfos.

        Payload structure after the header:
            [count:uint64][path_info_bytes + nar_bytes]...[0-size terminator]
        """
        fsrc = FramedReader(src)
        fdst = FramedWriter(dst)

        expected = await fsrc.read_uint64()
        fdst.write_uint64(expected)

        infos: set[OldValidPathInfo] = set()
        for _ in range(expected):
            info = await OldValidPathInfo.deserialize(ReadContext(reader=fsrc, version=1))
            infos.add(info)
            fdst.write(info.to_bytes())
            sent_bytes = 0
            while sent_bytes < info.nar_size:
                read = min(info.nar_size - sent_bytes, 1024 * 1024)
                data = await fsrc.readexactly(read)
                fdst.write(data)
                sent_bytes += len(data)

        await fdst.finalize()
        await fsrc.ensure_eof()
        return infos
