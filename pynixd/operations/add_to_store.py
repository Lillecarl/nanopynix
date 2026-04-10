"""AddToStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from pynixd.operations.sign_path_info import SignPathInfoRequest

from ..store_path import StorePath
from ..wire import NixReader, NixWriter, forward_framed
from .base import OpRequest, OpResponse, OperationLogs, PathInfo

if TYPE_CHECKING:
    from ..proxy import DaemonProxy

log = structlog.get_logger(__name__)


@dataclass
class AddToStoreResponse(OpResponse):
    """Response: ValidPathInfo (path + UnkeyedValidPathInfo)."""

    info: PathInfo = field(default_factory=PathInfo)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            logs=await OperationLogs.from_reader(reader),
            info=await PathInfo.from_reader_keyed(reader),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logs.to_writer(writer)
        await self.info.to_writer_keyed(writer)


@dataclass
class AddToStoreRequest(OpRequest[AddToStoreResponse]):
    """Prefix for AddToStore (framed NAR data follows)."""

    name: ClassVar[str] = "AddToStore"
    op: ClassVar[int] = 7
    response_type: ClassVar[type[OpResponse]] = AddToStoreResponse
    path_name: str = ""
    cam: str = ""  # ContentAddressMethodWithAlgo
    references: set[StorePath] = field(default_factory=set)
    repair: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            path_name=await reader.read_string(),
            cam=await reader.read_string(),
            references=await reader.read_string_set(StorePath),
            repair=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path_name)
        writer.write_string(self.cam)
        writer.write_string_set(self.references)
        writer.write_uint64(self.repair)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> AddToStoreResponse:
        """Override handle because this is a streaming operation."""
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        async with proxy.local_store.transfer_conn() as conn:
            await cls.forward(proxy.r, conn.w)
            await conn.w.drain()
            resp = await AddToStoreResponse.from_reader(conn.r, conn.version)
            resp.info = (
                await proxy.local_store.execute(SignPathInfoRequest(resp.info))
            ).info
            proxy.local_store.add_known_path(resp.info.path)
            proxy.local_store.add_path_info(resp.info)
            return resp

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> None:
        """Forward request prefix and stream framed NAR data from src to dst."""
        dst.write_uint64(7)

        name = await src.read_string()
        cam = await src.read_string()
        refs = await src.read_string_set(StorePath)
        repair = await src.read_uint64()

        dst.write_string(name)
        dst.write_string(cam)
        dst.write_string_set(refs)
        dst.write_uint64(repair)

        await forward_framed(src, dst)
