"""AddToStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from pynixd.operations.sign_path_info import SignPathInfoRequest

from ..store_path import StorePath
from ..wire import NixReader, NixWriter, forward_framed
from .base import (
    OperationLogs,
    OpRequest,
    OpResponse,
    RequestContext,
    ValidPathInfo,
)

if TYPE_CHECKING:
    pass


@dataclass
class AddToStoreResponse(OpResponse):
    """Response: ValidPathInfo (path + UnkeyedValidPathInfo)."""

    info: ValidPathInfo | None = None

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(reader)
        self.info = await ValidPathInfo().from_reader(reader)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", info=self.info)
        self.logs.to_writer(writer)
        if self.info is not None:
            self.info.to_writer(writer)


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

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.path_name = await reader.read_string()
        self.cam = await reader.read_string()
        self.references = await reader.read_string_set(StorePath)
        self.repair = await reader.read_uint64()
        self.logger.debug(
            "from_reader",
            path_name=self.path_name,
            cam=self.cam,
            references=self.references,
            repair=self.repair,
        )
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path_name)
        writer.write_string(self.cam)
        writer.write_string_set(self.references)
        writer.write_uint64(self.repair)

    async def handle(self, ctx: RequestContext) -> AddToStoreResponse:
        """Override handle because this is a streaming operation."""
        structlog.contextvars.bind_contextvars(operation=type(self).__name__)
        async with ctx.proxy.local_store.transfer_conn() as conn:
            await self.forward(ctx.proxy.r, conn.w)
            await conn.w.drain()
            resp = await AddToStoreResponse().from_reader(conn.r, conn.version)
            if resp.info is not None:
                resp.info = (
                    await ctx.proxy.local_store.execute(
                        SignPathInfoRequest(info=resp.info)
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
