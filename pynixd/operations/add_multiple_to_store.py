"""AddMultipleToStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..wire import FramedReader, FramedWriter, NixReader, NixWriter
from .base import OperationLogs, OpRequest, OpResponse, RequestContext, ValidPathInfo

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


@dataclass
class AddMultipleToStoreResponse(OpResponse):
    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        return cls(logs=logs)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
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

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        repair = await reader.read_uint64()
        dont_check_sigs = await reader.read_uint64()
        cls.logger.debug("from_reader", repair=repair, dont_check_sigs=dont_check_sigs)
        return cls(
            repair=repair,
            dont_check_sigs=dont_check_sigs,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_uint64(self.repair)
        writer.write_uint64(self.dont_check_sigs)

    @classmethod
    async def handle(cls, ctx: RequestContext) -> AddMultipleToStoreResponse:
        """Override handle because this is a streaming operation."""
        request = await cls.from_reader(ctx.proxy.r, ctx.version)
        async with ctx.proxy.local_store.transfer_conn() as conn:
            # Re-write the request prefix to the backend
            await request.to_writer(conn.w, conn.version)
            await conn.w.drain()

            infos = await cls.forward_stream(ctx.proxy.r, conn.w)
            resp = await AddMultipleToStoreResponse.from_reader(conn.r, conn.version)
            ctx.proxy.local_store.add_path_infos(infos)
            ctx.proxy.local_store.tracker.add_known_paths({i.path for i in infos})
        return resp

    @classmethod
    async def forward_stream(cls, src: NixReader, dst: NixWriter) -> set[ValidPathInfo]:
        """Forward AddMultipleToStore payload verbatim, snooping ValidPathInfos."""
        fsrc = FramedReader(src)
        fdst = FramedWriter(dst)

        expected = await fsrc.read_uint64()
        cls.logger.info("forward", expected_paths=expected)
        fdst.write_uint64(expected)

        infos: set[ValidPathInfo] = set()
        for _ in range(expected):
            info = await ValidPathInfo.from_reader(fsrc)
            infos.add(info)
            cls.logger.info(
                "forward_path_start", path=info.path, nar_size=info.nar_size
            )
            fdst.write(info.to_bytes())
            sent_bytes = 0
            while sent_bytes < info.nar_size:
                read = min(info.nar_size - sent_bytes, 1024 * 1024)
                data = await fsrc.readexactly(read)
                fdst.write(data)
                sent_bytes += len(data)
            cls.logger.info("forward_path_sent", sent_bytes=sent_bytes, path=info.path)

        await fsrc.ensure_eof()
        await fdst.finalize()
        return infos
