"""AddMultipleToStore operation request/response types."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import StorePath
from ..wire import NixReader, NixWriter, _nar_pad
from .base import OpRequest, OpResponse, OperationLogs, PathInfo

if TYPE_CHECKING:
    from ..proxy import DaemonProxy

log = structlog.get_logger(__name__)


@dataclass
class AddMultipleToStoreResponse(OpResponse):
    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(logs=await OperationLogs.from_reader(reader))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
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
        return cls(
            repair=await reader.read_uint64(),
            dont_check_sigs=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_uint64(self.repair)
        writer.write_uint64(self.dont_check_sigs)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> AddMultipleToStoreResponse:
        """Override handle because this is a streaming operation."""
        async with proxy.local_store.transfer_conn() as conn:
            paths = await cls.forward(proxy.r, conn.w)
            await conn.w.drain()
            await AddMultipleToStoreResponse.from_reader(conn.r, conn.version)
            proxy.local_store.add_known_paths(set(paths))
        return AddMultipleToStoreResponse()

    @classmethod
    async def forward(cls, src: NixReader, dst: NixWriter) -> list[StorePath]:
        """Forward AddMultipleToStore verbatim, snooping store paths."""
        dst.write_uint64(44)

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

        async def _read_string_set() -> set[str]:
            count = await _read_uint64()
            paths: set[str] = set()
            for _ in range(count):
                paths.add(await _read_string())
            return paths

        async def _read_path_set() -> set[StorePath]:
            count = await _read_uint64()
            paths: set[StorePath] = set()
            for _ in range(count):
                paths.add(await _read_store_path())
            return paths

        count = await _read_uint64()

        paths: list[StorePath] = []
        for _ in range(count):
            info = PathInfo(
                path=await _read_store_path(),
                deriver=await _read_store_path(),
                nar_hash=await _read_string(),
                references=await _read_path_set(),
                registration_time=await _read_uint64(),
                nar_size=await _read_uint64(),
                ultimate=await _read_uint64(),
                sigs=await _read_string_set(),
                ca=await _read_string(),
            )
            paths.append(info.path)

        cls.logger.debug("forward", paths=paths)

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
