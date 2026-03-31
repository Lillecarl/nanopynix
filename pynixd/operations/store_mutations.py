"""
Store mutation operation request/response types.

Subframe operations have a prefix (decoded here) and framed data
handled separately by the proxy.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..protocol import Op

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..store import Store
from ..wire import NixReader, NixWriter, _nar_pad, forward_framed
from .base import (
    EmptyResponse,
    OpRequest,
    OpResponse,
    PathInfo,
    SingleStringRequest,
    Uint64Response,
)

# ── AddToStore (subframe) ────────────────────────────────────────────


@dataclass
class AddToStoreResponse(OpResponse):
    """Response: ValidPathInfo (path + UnkeyedValidPathInfo)."""

    info: PathInfo = field(default_factory=PathInfo)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(info=await PathInfo.from_reader_keyed(reader))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        await self.info.to_writer_keyed(writer)


@dataclass
class AddToStoreRequest(OpRequest[AddToStoreResponse]):
    """Prefix for AddToStore (framed NAR data follows)."""

    op: ClassVar[int] = Op.AddToStore
    response_type: ClassVar[type[OpResponse]] = AddToStoreResponse
    name: str = ""
    cam: str = ""  # ContentAddressMethodWithAlgo
    references: set[str] = field(default_factory=set)
    repair: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            name=await reader.read_string(),
            cam=await reader.read_string(),
            references=await reader.read_string_set(),
            repair=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.name)
        writer.write_string(self.cam)
        writer.write_string_set(self.references)
        writer.write_uint64(self.repair)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> AddToStoreResponse:
        """Override handle because this is a streaming operation."""
        resp = await proxy.local_store.add_to_store_streaming(proxy._r)
        proxy.local_store.add_known_path(resp.info.path)
        return resp

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> None:
        """Forward the request prefix and then stream framed NAR data from src to dst.

        Writes Op.AddToStore, then prefix: name + cam + references + repair
        Then: framed NAR dump
        """
        dst.write_uint64(Op.AddToStore)

        name = await src.read_string()
        cam = await src.read_string()
        refs = await src.read_string_set()
        repair = await src.read_uint64()

        dst.write_string(name)
        dst.write_string(cam)
        dst.write_string_set(refs)
        dst.write_uint64(repair)

        await forward_framed(src, dst)


# ── AddToStoreNar (subframe) ─────────────────────────────────────────


# AddToStoreNar response: EmptyResponse


@dataclass
class AddToStoreNarRequest(OpRequest[EmptyResponse]):
    """Prefix for AddToStoreNar (framed NAR data follows)."""

    op: ClassVar[int] = Op.AddToStoreNar
    response_type: ClassVar[type[OpResponse]] = EmptyResponse
    info: PathInfo = field(default_factory=PathInfo)
    repair: int = 0
    dont_check_sigs: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        info = PathInfo(
            path=await reader.read_string(),
            deriver=await reader.read_string(),
            nar_hash=await reader.read_string(),
            references=await reader.read_string_set(),
            registration_time=await reader.read_uint64(),
            nar_size=await reader.read_uint64(),
            ultimate=await reader.read_uint64(),
            sigs=await reader.read_string_set(),
            ca=await reader.read_string(),
        )
        return cls(
            info=info,
            repair=await reader.read_uint64(),
            dont_check_sigs=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.info.path)
        writer.write_string(self.info.deriver)
        writer.write_string(self.info.nar_hash)
        writer.write_string_set(self.info.references)
        writer.write_uint64(self.info.registration_time)
        writer.write_uint64(self.info.nar_size)
        writer.write_uint64(self.info.ultimate)
        writer.write_string_set(self.info.sigs)
        writer.write_string(self.info.ca)
        writer.write_uint64(self.repair)
        writer.write_uint64(self.dont_check_sigs)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> EmptyResponse:
        """Override handle because this is a streaming operation."""
        path = await proxy.local_store.add_to_store_nar_streaming(proxy._r)
        proxy.local_store.add_known_path(path)
        return EmptyResponse()

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> str:
        """Forward the request prefix and then stream framed NAR data from src to dst.

        Writes Op.AddToStoreNar, then prefix fields, then framed NAR dump.
        Returns the store path extracted from the request.
        """
        dst.write_uint64(Op.AddToStoreNar)

        path = await src.read_string()
        deriver = await src.read_string()
        nar_hash = await src.read_string()
        refs = await src.read_string_set()
        reg_time = await src.read_uint64()
        nar_size = await src.read_uint64()
        ultimate = await src.read_uint64()
        sigs = await src.read_string_set()
        ca = await src.read_string()
        repair = await src.read_uint64()
        dont_check_sigs = await src.read_uint64()

        dst.write_string(path)
        dst.write_string(deriver)
        dst.write_string(nar_hash)
        dst.write_string_set(refs)
        dst.write_uint64(reg_time)
        dst.write_uint64(nar_size)
        dst.write_uint64(ultimate)
        dst.write_string_set(sigs)
        dst.write_string(ca)
        dst.write_uint64(repair)
        dst.write_uint64(dont_check_sigs)

        await forward_framed(src, dst)

        return path


# ── AddMultipleToStore (subframe) ────────────────────────────────────


# AddMultipleToStore response: EmptyResponse


@dataclass
class AddMultipleToStoreRequest(OpRequest[EmptyResponse]):
    """Prefix for AddMultipleToStore (framed data follows)."""

    op: ClassVar[int] = Op.AddMultipleToStore
    response_type: ClassVar[type[OpResponse]] = EmptyResponse
    repair: int = 0
    dont_check_sigs: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            repair=await reader.read_uint64(),
            dont_check_sigs=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.repair)
        writer.write_uint64(self.dont_check_sigs)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> EmptyResponse:
        """Override handle because this is a streaming operation."""
        paths = await proxy.local_store.add_multiple_to_store_streaming(proxy._r)
        proxy.local_store.add_known_paths(set(paths))
        return EmptyResponse()

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> list[str]:
        """Forward AddMultipleToStore verbatim, snooping store paths.

        Forwards frame chunks as-is from src to dst while parsing just
        enough of the logical stream to extract store paths and skip
        NAR data without buffering it.

        Framed payload structure (logical stream):
            uint64 count
            for each entry:
                ValidPathInfo (keyed: path + unkeyed fields)
                raw NAR (nar_size bytes, not padded)
        """
        dst.write_uint64(Op.AddMultipleToStore)

        repair = await src.read_uint64()
        dont_check_sigs = await src.read_uint64()

        dst.write_uint64(repair)
        dst.write_uint64(dont_check_sigs)

        return await _forward_framed_snooping(src, dst)


# ── AddSignatures ────────────────────────────────────────────────────


@dataclass
class AddSignaturesRequest(OpRequest[Uint64Response]):
    op: ClassVar[int] = Op.AddSignatures
    response_type: ClassVar[type[OpResponse]] = Uint64Response
    path: str = ""
    sigs: set[str] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            path=await reader.read_string(),
            sigs=await reader.read_string_set(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.path)
        writer.write_string_set(self.sigs)


# ── Thin request subclasses for SingleStringRequest-based ops ──────────


# ── AddBuildLog (subframe) ───────────────────────────────────────────
# Request prefix: SingleStringRequest (path)
# Response: Uint64Response


@dataclass
class AddBuildLogRequest(SingleStringRequest[Uint64Response]):
    op: ClassVar[int] = Op.AddBuildLog
    response_type: ClassVar[type[OpResponse]] = Uint64Response

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> Uint64Response:
        # Consumes framed data that follows if it was a real daemon,
        # but pynixd proxy loop handles the framing if it's marked as subframe.
        # However, Op.AddBuildLog is NOT a subframe op in the protocol sense
        # that it has a framed body following the request.
        # TODO: Inform client about ignoring this if relevant.
        return Uint64Response(value=0)


@dataclass
class AddTempRootRequest(SingleStringRequest[Uint64Response]):
    op: ClassVar[int] = Op.AddTempRoot
    response_type: ClassVar[type[OpResponse]] = Uint64Response

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> Uint64Response:
        # We don't want clients creating temp roots on our host.
        # TODO: Inform client about ignoring this if relevant.
        return Uint64Response(value=0)


@dataclass
class AddIndirectRootRequest(SingleStringRequest[Uint64Response]):
    op: ClassVar[int] = Op.AddIndirectRoot
    response_type: ClassVar[type[OpResponse]] = Uint64Response

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> Uint64Response:
        # No-op: return success (0).
        # TODO: Inform client about ignoring this if relevant.
        return Uint64Response(value=0)


@dataclass
class EnsurePathRequest(SingleStringRequest[Uint64Response]):
    op: ClassVar[int] = Op.EnsurePath
    response_type: ClassVar[type[OpResponse]] = Uint64Response


# ── Forwarding helpers ─────────────────────────────────────────────


async def _forward_framed_snooping(
    src: NixReader,
    dst: NixWriter,
) -> list[str]:
    """Forward framed data verbatim while extracting store paths.

    Reads frame chunks from src and writes them to dst unchanged.
    Maintains a small parse buffer over the logical stream to read
    structured PathInfo fields (path, nar_size), then skips past
    NAR bytes without buffering them.

    Wire layout of the logical (deframed) stream:
        uint64 count
        for each entry:
            string path          <- extracted
            string deriver       <- skipped
            string nar_hash      <- skipped
            string_set refs      <- skipped
            uint64 reg_time      <- skipped
            uint64 nar_size      <- extracted (to know how much NAR to skip)
            uint64 ultimate      <- skipped
            string_set sigs      <- skipped
            string ca            <- skipped
            bytes[nar_size] NAR  <- skipped (not padded)
    """
    buf = bytearray()
    eof = False

    async def _ensure(n: int) -> None:
        """Read frame chunks until buf has at least n bytes."""
        nonlocal buf, eof
        while len(buf) < n:
            if eof:
                raise EOFError("Framed stream ended prematurely")
            size = await src.read_uint64()
            if size == 0:
                eof = True
                dst.write_uint64(0)
                if len(buf) < n:
                    raise EOFError("Framed stream ended prematurely")
                return
            data = await src.readexactly(size)
            dst.write_uint64(size)
            dst.write(data)
            buf.extend(data)

    def _consume(n: int) -> bytes:
        nonlocal buf
        result = bytes(buf[:n])
        del buf[:n]
        return result

    async def _skip(n: int) -> None:
        """Skip n logical bytes, consuming from buf first."""
        nonlocal buf, eof
        # Consume whatever is already buffered
        from_buf = min(len(buf), n)
        if from_buf:
            del buf[:from_buf]
            n -= from_buf
        # Skip remaining by reading+forwarding frame chunks
        while n > 0:
            if eof:
                raise EOFError("Framed stream ended prematurely")
            size = await src.read_uint64()
            if size == 0:
                eof = True
                dst.write_uint64(0)
                raise EOFError("Framed stream ended prematurely")
            data = await src.readexactly(size)
            dst.write_uint64(size)
            dst.write(data)
            if size <= n:
                # Entire chunk is part of the skip
                n -= size
            else:
                # Chunk has data beyond what we're skipping
                buf.extend(data[n:])
                n = 0

    async def _read_uint64() -> int:
        await _ensure(8)
        return struct.unpack("<Q", _consume(8))[0]

    async def _read_string() -> str:
        length = await _read_uint64()
        padded = length + _nar_pad(length)
        await _ensure(padded)
        data = _consume(padded)
        return data[:length].decode("utf-8")

    async def _skip_string() -> None:
        length = await _read_uint64()
        padded = length + _nar_pad(length)
        await _skip(padded)

    async def _skip_string_set() -> None:
        count = await _read_uint64()
        for _ in range(count):
            await _skip_string()

    # Parse the logical stream
    count = await _read_uint64()

    paths: list[str] = []
    for _ in range(count):
        path = await _read_string()
        paths.append(path)
        await _skip_string()  # deriver
        await _skip_string()  # nar_hash
        await _skip_string_set()  # references
        await _skip(8)  # registration_time
        nar_size = await _read_uint64()
        await _skip(8)  # ultimate
        await _skip_string_set()  # sigs
        await _skip_string()  # ca
        await _skip(nar_size)  # raw NAR (not padded in framed)

    # Forward any remaining frames
    if not eof:
        while True:
            size = await src.read_uint64()
            if size == 0:
                dst.write_uint64(0)
                break
            data = await src.readexactly(size)
            dst.write_uint64(size)
            dst.write(data)

    await dst.drain()
    return paths
