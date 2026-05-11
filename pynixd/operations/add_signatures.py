"""AddSignatures operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.context import ReadContext, WriteContext
    from ..wire import NixReader, NixWriter


@dataclass
class AddSignaturesResponse(OpResponse):
    value: int

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
        obj.value = await reader.read_uint64()
        return obj

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.value = await ctx.reader.read_uint64()
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("to_writer", value=self.value)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(self.value)


@dataclass(kw_only=True)
class AddSignaturesRequest(OpRequest[AddSignaturesResponse]):
    name: ClassVar[str] = "AddSignatures"
    op: ClassVar[int] = 37
    response_type: ClassVar[type[OpResponse]] = AddSignaturesResponse
    path: str
    sigs: set[str]

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.path = await reader.read_string(StorePath)
        obj.sigs = await reader.read_string_set()
        obj.logger.debug("from_reader", path=obj.path, sigs=obj.sigs)
        return obj

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.path = await ctx.reader.read_string(StorePath)
        obj.sigs = await ctx.reader.read_string_set()
        obj.logger.debug("from_reader", path=obj.path, sigs=obj.sigs)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.path)
        writer.write_string_set(self.sigs)

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.path)
        ctx.writer.write_string_set(self.sigs)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> AddSignaturesResponse:
        resp = await store.call(self, client=client, suppress_last=suppress_last)
        if resp.value == 1:
            # Signatures added — invalidate cached path info so the next
            # QueryPathInfo reads fresh data from the DB.
            store.path_info_cache.pop(StorePath(self.path), None)
        return resp
