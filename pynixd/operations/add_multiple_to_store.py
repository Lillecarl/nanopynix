"""AddMultipleToStore operation request/response types."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter, _nar_pad
from .base import EmptyResponse, OpRequest, OpResponse

if TYPE_CHECKING:
    from ..proxy import DaemonProxy

log = structlog.get_logger(__name__)


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
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        async with proxy.local_store.transfer_conn() as conn:
            paths = await cls.forward(proxy.r, conn.w)
            await conn.w.drain()
            await conn.r.drain_stderr()
            await EmptyResponse.from_reader(conn.r, conn.version)
            proxy.local_store.add_known_paths(set(paths))
        return EmptyResponse()

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> list[StorePath]:
        """Forward AddMultipleToStore verbatim, snooping store paths."""
        dst.write_uint64(Op.AddMultipleToStore)

        repair = await src.read_uint64()
        dont_check_sigs = await src.read_uint64()

        dst.write_uint64(repair)
        dst.write_uint64(dont_check_sigs)

        buf = bytearray()
        eof = False

        async def _ensure(n: int) -> None:
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
            nonlocal buf, eof
            from_buf = min(len(buf), n)
            if from_buf:
                del buf[:from_buf]
                n -= from_buf
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
                    n -= size
                else:
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

        async def _read_store_path() -> StorePath:
            return StorePath(await _read_string())

        async def _skip_string() -> None:
            length = await _read_uint64()
            padded = length + _nar_pad(length)
            await _skip(padded)

        async def _skip_string_set() -> None:
            count = await _read_uint64()
            for _ in range(count):
                await _skip_string()

        count = await _read_uint64()

        paths: list[StorePath] = []
        for _ in range(count):
            path = await _read_store_path()
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
