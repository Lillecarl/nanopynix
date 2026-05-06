"""AddPermRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs, StderrNext
from ..types.auth import Role
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..types import RequestContext as RequestContext
    from ..wire import NixReader, NixWriter


@dataclass
class AddPermRootResponse(OpResponse):
    gc_root: str

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
        await obj.logs.from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        obj.gc_root = await reader.read_string()
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", gc_root=self.gc_root)
        self.logs.to_writer(writer)
        writer.write_string(self.gc_root)


@dataclass(kw_only=True)
class AddPermRootRequest(OpRequest[AddPermRootResponse]):
    name: ClassVar[str] = "AddPermRoot"
    op: ClassVar[int] = 47
    response_type: ClassVar[type[OpResponse]] = AddPermRootResponse
    store_path: str
    gc_root: str

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,  # noqa: ARG003
        buffer_logs: bool = True,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.store_path = await reader.read_string()
        obj.gc_root = await reader.read_string()
        obj.logger.debug(
            "from_reader",
            store_path=obj.store_path,
            gc_root=obj.gc_root,
        )
        return obj

    async def handle(self, ctx: RequestContext) -> AddPermRootResponse | None:
        self = await self.from_reader(ctx.proxy.r, ctx.version)
        if ctx.proxy.role == Role.ADMIN:
            return await ctx.proxy.execute(self)

        resp = AddPermRootResponse(gc_root=self.gc_root)
        msg = StderrNext(f"pynixd: AddPermRoot {self.store_path} ignored (no-op)")
        resp.logs.add(msg)
        return resp

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.store_path)
        writer.write_string(self.gc_root)
