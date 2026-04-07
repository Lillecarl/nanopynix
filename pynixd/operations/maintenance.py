"""
Maintenance operation request/response types.

OptimiseStore, VerifyStore, FindRoots, AddPermRoot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..protocol import Op

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
from ..wire import NixReader, NixWriter
from .base import (
    EmptyRequest,
    OpRequest,
    OpResponse,
    Uint64Response,
)

# ── VerifyStore ──────────────────────────────────────────────────────


@dataclass
class VerifyStoreRequest(OpRequest[Uint64Response]):
    op: ClassVar[int] = Op.VerifyStore
    response_type: ClassVar[type[OpResponse]] = Uint64Response
    check_contents: int = 0
    repair: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            check_contents=await reader.read_uint64(),
            repair=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.check_contents)
        writer.write_uint64(self.repair)


# ── OptimiseStore ────────────────────────────────────────────────────


@dataclass
class OptimiseStoreRequest(EmptyRequest[Uint64Response]):
    op: ClassVar[int] = Op.OptimiseStore
    response_type: ClassVar[type[OpResponse]] = Uint64Response


# ── FindRoots ────────────────────────────────────────────────────────


@dataclass
class FindRootsEntry:
    link: str = ""
    target: str = ""


@dataclass
class FindRootsResponse(OpResponse):
    roots: list[FindRootsEntry] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        roots = []
        for _ in range(n):
            link = await reader.read_string()
            target = await reader.read_string()
            roots.append(FindRootsEntry(link=link, target=target))
        return cls(roots=roots)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.roots))
        for root in self.roots:
            writer.write_string(root.link)
            writer.write_string(root.target)


@dataclass
class FindRootsRequest(EmptyRequest[FindRootsResponse]):
    op: ClassVar[int] = Op.FindRoots
    response_type: ClassVar[type[OpResponse]] = FindRootsResponse


# ── AddPermRoot ─────────────────────────────────────────────────────
# Nix 1.38+. Request: store path + gcRoot path. Response: gcRoot path.
# pynixd discards this — clients shouldn't create permanent roots.


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
        # TODO: We might want to tell the client that we're ignoring this
        # or parts of it at some point.
        return AddPermRootResponse(gc_root=self.gc_root)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.store_path)
        writer.write_string(self.gc_root)
