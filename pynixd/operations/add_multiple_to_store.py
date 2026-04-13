"""AddMultipleToStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import StorePath
from ..wire import NixReader, NixWriter, FramedReader, FramedWriter
from .base import OpRequest, OpResponse, OperationLogs, PathInfo

if TYPE_CHECKING:
    from ..proxy import DaemonProxy

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
    async def handle(cls, proxy: DaemonProxy) -> AddMultipleToStoreResponse:
        """Override handle because this is a streaming operation."""
        async with proxy.local_store.transfer_conn() as conn:
            infos = await cls.forward(proxy.r, conn.w)
            resp = await AddMultipleToStoreResponse.from_reader(conn.r, conn.version)
            proxy.local_store.add_path_infos(infos)
            proxy.local_store.add_known_paths({i.path for i in infos})
        return resp

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> set[PathInfo]:
        """Forward AddMultipleToStore verbatim, snooping PathInfos."""
        dst.write_uint64(44)

        repair = await src.read_uint64()
        dont_check_sigs = await src.read_uint64()

        dst.write_uint64(repair)
        dst.write_uint64(dont_check_sigs)

        fsrc = FramedReader(src)
        fdst = FramedWriter(dst)

        expected = await fsrc.read_uint64()
        cls.logger.info(f"forward: expected {expected} paths")
        fdst.write_uint64(expected)

        infos: set[PathInfo] = set()
        for i in range(expected):
            info = await PathInfo.from_reader_keyed(fsrc)
            infos.add(info)
            cls.logger.info(f"forward: path {info.path} nar_size={info.nar_size}")
            fdst.write(info.to_bytes())
            sent_bytes = 0
            while sent_bytes < info.nar_size:
                read = min(info.nar_size - sent_bytes, 1024 * 1024)
                data = await fsrc.readexactly(read)
                fdst.write(data)
                sent_bytes += len(data)
            cls.logger.info(f"forward: sent {sent_bytes} bytes for {info.path}")

        await fdst.finalize()
        return infos
