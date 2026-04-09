"""AddPermRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..protocol import Op
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class AddPermRootResponse(OpResponse):
    gc_root: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(gc_root=await reader.read_string())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.gc_root)


@dataclass
class AddPermRootRequest(OpRequest[AddPermRootResponse]):
    name: ClassVar[str] = "AddPermRoot"
    op: ClassVar[int] = Op.AddPermRoot
    response_type: ClassVar[type[OpResponse]] = AddPermRootResponse
    store_path: str = ""
    gc_root: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            store_path=await reader.read_string(),
            gc_root=await reader.read_string(),
        )

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> AddPermRootResponse:
        # No-op: don't create permanent roots on the host.
        # Just return the requested root path as "success".
        return AddPermRootResponse(gc_root=self.gc_root)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.store_path)
        writer.write_string(self.gc_root)
