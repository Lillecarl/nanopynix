"""
Nix daemon wire protocol primitives (async).

All integers are little-endian uint64. Strings are length-prefixed with
zero-padding to 8-byte alignment.

Read functions are async (for asyncio.StreamReader / asyncssh.SSHReader).
Write functions are sync (writer.write() buffers; callers await drain()).
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Final

import asyncssh
from environs import env


_CHUNK_SIZE = env.int("PYNIXD_CHUNK_SIZE", 1024 * 1024)

_SSH_WINDOW_SIZE = env.int("PYNIXD_SSH_WINDOW", 16 * 1024 * 1024)

if TYPE_CHECKING:
    from . import stderr


def _nar_pad(n: int) -> int:
    """Return number of padding bytes needed for 8-byte alignment."""
    return (8 - n % 8) % 8


# ── NixReader / NixWriter ──────────────────────────────────────


class NixReader:
    """Wraps AsyncReader with wire protocol methods and dirty checking."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def _compact(self) -> None:
        if self._pos > 0:
            del self._buf[: self._pos]
            self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        # Fast path: serve entirely from local buffer
        if len(self._buf) - self._pos >= n:
            result = bytes(self._buf[self._pos : self._pos + n])
            self._pos += n
            if self._pos > 64 * 1024:
                self._compact()
            return result
        return await self._read_more(n)

    async def _read_more(self, n: int) -> bytes:
        # Need more data from the stream
        needed = n - (len(self._buf) - self._pos)
        read_ahead = self._get_read_ahead()

        if needed <= read_ahead:
            # Small read: try to grab extra data to fill future reads
            data = await self._read_from_transport(max(needed, read_ahead))
            if not data:
                raise EOFError("Stream closed")
            self._buf.extend(data)
            # May still not have enough if stream had less available
            while len(self._buf) - self._pos < n:
                data = await self._readexactly_from_transport(
                    n - (len(self._buf) - self._pos)
                )
                self._buf.extend(data)
            result = bytes(self._buf[self._pos : self._pos + n])
            self._pos += n
            if self._pos > 64 * 1024:
                self._compact()
            return result
        else:
            # Large read (NAR data etc.): read exactly what's needed
            if len(self._buf) - self._pos > 0:
                already_have = bytes(self._buf[self._pos :])
                self._buf = bytearray()
                self._pos = 0
                needed = n - len(already_have)
                return already_have + await self._readexactly_from_transport(needed)

            return await self._readexactly_from_transport(n)

    def _get_read_ahead(self) -> int:
        return 0

    async def _read_from_transport(self, n: int) -> bytes:
        return await self._readexactly_from_transport(n)

    async def _readexactly_from_transport(self, n: int) -> bytes:
        raise NotImplementedError

    async def read_uint64(self) -> int:
        if len(self._buf) - self._pos >= 8:
            val = _UINT64_STRUCT.unpack_from(self._buf, self._pos)[0]
            self._pos += 8
            if self._pos > 64 * 1024:
                self._compact()
            return val
        return _UINT64_STRUCT.unpack(await self.readexactly(8))[0]

    async def read_bool(self) -> bool:
        return await self.read_uint64() != 0

    async def read_optional_uint64(self) -> int | None:
        tag = await self.read_uint64()
        if tag == 0:
            return None
        return await self.read_uint64()

    async def read_bytes(self) -> bytes:
        if len(self._buf) - self._pos >= 8:
            length = _UINT64_STRUCT.unpack_from(self._buf, self._pos)[0]
            pad = _nar_pad(length)
            total = 8 + length + pad
            if len(self._buf) - self._pos >= total:
                data = bytes(self._buf[self._pos + 8 : self._pos + 8 + length])
                self._pos += total
                if self._pos > 64 * 1024:
                    self._compact()
                return data

        length = await self.read_uint64()
        data = await self.readexactly(length)
        pad = _nar_pad(length)
        if pad:
            await self.readexactly(pad)
        return data

    async def read_string[T: str = str](self, tp: type[T] = str) -> T:
        return tp((await self.read_bytes()).decode("utf-8"))

    async def read_string_list[T: str = str](self, tp: type[T] = str) -> list[T]:
        count: int = await self.read_uint64()
        return [await self.read_string(tp) for _ in range(count)]

    async def read_string_set[T: str = str](self, tp: type[T] = str) -> set[T]:
        count: int = await self.read_uint64()
        return {await self.read_string(tp) for _ in range(count)}

    async def drain_stderr(self, raise_on_error: bool = True) -> None:
        """Read and discard all stderr messages until STDERR_LAST."""
        from . import stderr

        await stderr.drain(self, raise_on_error=raise_on_error)

    def read_stderr(self) -> AsyncIterator[stderr.StderrMsg]:
        """Return an AsyncIterator that yields stderr messages until STDERR_LAST."""
        from . import stderr

        return stderr.read_stream(self)

    async def is_dirty(self) -> bool:
        if len(self._buf) - self._pos > 0:
            return True
        return self._transport_is_dirty()

    def _transport_is_dirty(self) -> bool:
        return False


_SSH_READ_AHEAD = 16 * 1024  # read-ahead size to amortize asyncssh lock overhead


class SSHNixReader(NixReader):
    def __init__(self, reader: asyncssh.SSHReader) -> None:
        super().__init__()
        self.reader = reader

    def _get_read_ahead(self) -> int:
        return _SSH_READ_AHEAD

    async def _read_from_transport(self, n: int) -> bytes:
        try:
            return await self.reader.read(n)
        except asyncssh.misc.ConnectionLost:
            raise EOFError("SSH connection lost")

    async def _readexactly_from_transport(self, n: int) -> bytes:
        try:
            return await self.reader.readexactly(n)
        except asyncssh.misc.ConnectionLost:
            raise EOFError("SSH connection lost")

    def _transport_is_dirty(self) -> bool:
        if hasattr(self.reader, "get_read_buffer_size"):
            return self.reader.get_read_buffer_size() > 0  # type: ignore[reportAttributeAccessIssue]
        # Fallback to private access if API changed or is missing
        # _recv_buf is a dict of list of bytes
        buf = self.reader._session._recv_buf.get(
            self.reader._datatype,
            [],
        )
        for chunk in buf:
            if isinstance(chunk, (bytes, bytearray)) and len(chunk) > 0:
                return True
        return False


_UNIX_READ_AHEAD = 16 * 1024  # read-ahead size to amortize syscall overhead


_UINT64_STRUCT = struct.Struct("<Q")


class UnixNixReader(NixReader):
    def __init__(self, reader: asyncio.StreamReader) -> None:
        super().__init__()
        self.reader = reader

    def _get_read_ahead(self) -> int:
        return _UNIX_READ_AHEAD

    async def _read_from_transport(self, n: int) -> bytes:
        return await self.reader.read(n)

    async def _readexactly_from_transport(self, n: int) -> bytes:
        return await self.reader.readexactly(n)

    def _transport_is_dirty(self) -> bool:
        # Check internal buffer without consuming data
        return len(self.reader._buffer) > 0  # type: ignore[attr-defined]


class NixWriter:
    """Wraps AsyncWriter with wire protocol methods."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        """Write raw bytes, utilizing buffering if configured."""
        chunk_size = self._get_chunk_size()
        read_ahead = self._get_read_ahead()

        if chunk_size and len(data) >= chunk_size:
            if self._buf:
                self._write_to_transport(bytes(self._buf))
                self._buf.clear()
            self._write_to_transport(data)
        else:
            self._buf.extend(data)
            if read_ahead and len(self._buf) >= read_ahead:
                self._write_to_transport(bytes(self._buf))
                self._buf.clear()

    async def drain(self) -> None:
        """Flush writer."""
        if self._buf:
            self._write_to_transport(bytes(self._buf))
            self._buf.clear()
        await self._drain_transport()

    async def is_dirty(self) -> bool:
        """Check if writer has un-drained data."""
        if self._buf:
            return True
        return self._transport_is_dirty()

    def _get_chunk_size(self) -> int:
        return 0

    def _get_read_ahead(self) -> int:
        return 0

    def _write_to_transport(self, data: bytes) -> None:
        pass

    async def _drain_transport(self) -> None:
        pass

    def _transport_is_dirty(self) -> bool:
        return False

    async def close(self) -> None:
        """Close the underlying transport. Override in subclasses."""

    def write_uint64(self, val: int) -> None:
        self.write(_UINT64_STRUCT.pack(val))

    def write_uint64s(self, vals: list[int]) -> None:
        if not vals:
            return
        self.write(struct.pack(f"<{len(vals)}Q", *vals))

    def write_bool(self, val: bool) -> None:
        self.write_uint64(1 if val else 0)

    def write_optional_uint64(self, val: int | None) -> None:
        if val is None:
            self.write_uint64(0)
        else:
            self.write_uint64(1)
            self.write_uint64(val)

    def write_bytes(self, data: bytes) -> None:
        self.write_uint64(len(data))
        self.write(data)
        pad: int = _nar_pad(len(data))
        if pad:
            self.write(b"\0" * pad)

    def write_string(self, s: str) -> None:
        self.write_bytes(s.encode("utf-8"))

    def write_string_list(self, items: Iterable[str]) -> None:
        items_list = list(items)
        self.write_uint64(len(items_list))
        for item in items_list:
            self.write_string(item)

    def write_string_set(self, items: Iterable[str]) -> None:
        self.write_string_list(items)

    def framed(self, chunk_size: int = _CHUNK_SIZE) -> FramedWriter:
        """Create a FramedWriter that writes framed data to this writer."""
        return FramedWriter(self, chunk_size)


class SSHNixWriter(NixWriter):
    def __init__(self, writer: asyncssh.SSHWriter) -> None:
        super().__init__()
        self.writer = writer

    def _get_chunk_size(self) -> int:
        return _CHUNK_SIZE

    def _get_read_ahead(self) -> int:
        return _SSH_READ_AHEAD

    def _write_to_transport(self, data: bytes) -> None:
        self.writer.write(data)

    async def _drain_transport(self) -> None:
        await self.writer.drain()

    def _transport_is_dirty(self) -> bool:
        return self.writer.channel.get_write_buffer_size() > 0

    async def close(self) -> None:
        await self.drain()
        self.writer.close()


class UnixNixWriter(NixWriter):
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        super().__init__()
        self.writer = writer

    def _get_chunk_size(self) -> int:
        return _CHUNK_SIZE

    def _get_read_ahead(self) -> int:
        return _UNIX_READ_AHEAD

    def _write_to_transport(self, data: bytes) -> None:
        self.writer.write(data)

    async def _drain_transport(self) -> None:
        await self.writer.drain()

    def _transport_is_dirty(self) -> bool:
        return self.writer.transport.get_write_buffer_size() > 0

    async def close(self) -> None:
        await self.drain()
        self.writer.close()
        await self.writer.wait_closed()


WORKER_MAGIC_1: Final[int] = 0x6E697863  # client hello
WORKER_MAGIC_2: Final[int] = 0x6478696F  # server hello
PROTOCOL_VERSION: Final[int] = (1 << 8) | 35  # 1.35


def proto(major: int, minor: int) -> int:
    """Encode a protocol version as a single int."""
    return (major << 8) | minor


def proto_str(version: int) -> str:
    """Format a protocol version int as 'major.minor'."""
    return f"{version >> 8}.{version & 0xFF}"


STDERR_NEXT: Final[int] = 0x6F6C6D67
STDERR_LAST: Final[int] = 0x616C7473
STDERR_ERROR: Final[int] = 0x63787470
STDERR_START_ACTIVITY: Final[int] = 0x53545254
STDERR_STOP_ACTIVITY: Final[int] = 0x53544F50
STDERR_RESULT: Final[int] = 0x52534C54


async def stream_parse_nar(
    src: NixReader,
    dst: NixWriter,
    capture: bool = False,
) -> bytes | None:
    """Copy a NAR archive from src to dst by parsing the NAR structure.

    The NAR format is a sequence of length-prefixed, 8-byte-padded tokens.
    We track "(" / ")" depth to know when the NAR ends.
    The token after "contents" is binary file data and must not be interpreted.

    If capture=True, returns the raw NAR bytes.
    """
    chunks: list[bytes] | None = [] if capture else None

    async def _fwd_token() -> bytes:
        """Read and forward one token, returning its raw data."""
        length: int = await src.read_uint64()
        dst.write_uint64(length)
        if chunks is not None:
            chunks.append(struct.pack("<Q", length))

        data: bytes = b""
        if length > 0:
            data = await src.readexactly(length)
            dst.write(data)
            if chunks is not None:
                chunks.append(data)

        pad: int = _nar_pad(length)
        if pad:
            pad_data: bytes = await src.readexactly(pad)
            dst.write(pad_data)
            if chunks is not None:
                chunks.append(pad_data)

        return data

    depth: int = 0
    after_contents: bool = False

    while True:
        raw: bytes = await _fwd_token()

        # Token after "contents" is binary file data — skip interpretation
        if after_contents:
            after_contents = False
            continue

        try:
            tok: str = raw.decode("ascii")
        except (UnicodeDecodeError, ValueError):
            continue

        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0:
                break
        elif tok == "contents":
            after_contents = True

    if chunks is not None:
        return b"".join(chunks)
    return None


async def discard_nar(src: NixReader) -> None:
    """Discard a NAR archive by reading until it ends.

    The NAR format is self-delimiting. We track "(" / ")" depth to know
    when the NAR ends. The token after "contents" is binary file data.
    """
    depth: int = 0
    after_contents: bool = False

    while True:
        length: int = await src.read_uint64()
        if length > 0:
            data: bytes = await src.readexactly(length)
            pad: int = _nar_pad(length)
            if pad:
                await src.readexactly(pad)
        else:
            data = b""

        if after_contents:
            after_contents = False
            continue

        try:
            tok: str = data.decode("ascii")
        except (UnicodeDecodeError, ValueError):
            continue

        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0:
                break
        elif tok == "contents":
            after_contents = True


async def forward_framed(src: NixReader, dst: NixWriter) -> None:
    """Forward framed data (chunks terminated by size=0) from src to dst.

    Streams the data without buffering the entire payload in memory.
    Each chunk is: uint64 length + raw data, terminated by length=0.
    """
    while True:
        size = await src.read_uint64()
        if size == 0:
            dst.write_uint64(0)
            break
        data = await src.readexactly(size)
        dst.write_uint64(size)
        dst.write(data)
    await dst.drain()


async def discard_framed(src: NixReader) -> None:
    """Read and discard framed data (chunks terminated by size=0)."""
    while True:
        size = await src.read_uint64()
        if size == 0:
            break
        await src.readexactly(size)


class FramedReader(NixReader):
    """Reassembles framed chunks into a logical byte stream.

    Reads [uint64 size][data]... [uint64 0] from a DaemonReader and
    presents a readexactly() interface. Inherits from NixReader so it
    can be passed directly to code expecting a NixReader.

    This allows parsing structured data (PathInfo, NAR tokens, etc.)
    from inside framed payloads without buffering the entire payload.
    """

    def __init__(self, src: NixReader) -> None:
        super().__init__()
        self._src = src
        self._eof = False

    async def _fill(self, needed: int) -> None:
        """Read framed chunks until buffer has at least `needed` bytes."""
        while (len(self._buf) - self._pos) < needed:
            if self._eof:
                have = len(self._buf) - self._pos
                raise EOFError(f"Framed stream ended, need {needed} bytes, have {have}")
            size = await self._src.read_uint64()
            if size == 0:
                self._eof = True
                have = len(self._buf) - self._pos
                if have < needed:
                    raise EOFError(
                        f"Framed stream ended, need {needed} bytes, have {have}"
                    )
                return
            data = await self._src.readexactly(size)
            self._buf.extend(data)

    async def _read_more(self, n: int) -> bytes:
        await self._fill(n)
        result = bytes(self._buf[self._pos : self._pos + n])
        self._pos += n
        if self._pos > 64 * 1024:
            self._compact()
        return result

    async def is_dirty(self) -> bool:
        if len(self._buf) - self._pos > 0:
            return True
        return await self._src.is_dirty()

    @property
    def at_eof(self) -> bool:
        return self._eof and (len(self._buf) - self._pos) == 0

    async def drain_remaining(self) -> None:
        """Discard remaining framed data (if stream wasn't fully consumed)."""
        if self._eof:
            return
        while True:
            size = await self._src.read_uint64()
            if size == 0:
                self._eof = True
                return
            await self._src.readexactly(size)


class FramedWriter(NixWriter):
    """Buffers writes and emits framed chunks to an underlying writer.

    Framed format: [uint64 size][data]... [uint64 0] (terminator).
    Call finalize() when done to flush remaining data and write terminator.
    """

    def __init__(
        self,
        dst: NixWriter,
        chunk_size: int = _CHUNK_SIZE,
    ) -> None:
        super().__init__()
        self._dst = dst
        self._chunk_size = chunk_size

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        while len(self._buf) >= self._chunk_size:
            chunk = bytes(self._buf[: self._chunk_size])
            self._dst.write_uint64(self._chunk_size)
            self._dst.write(chunk)
            del self._buf[: self._chunk_size]

    async def drain(self) -> None:
        """No-op — actual flushing happens in write() and finalize()."""
        pass

    async def finalize(self) -> None:
        """Flush remaining buffer and write framing terminator."""
        if self._buf:
            self._dst.write_uint64(len(self._buf))
            self._dst.write(bytes(self._buf))
            self._buf = bytearray()
        self._dst.write_uint64(0)  # terminator
        await self._dst.drain()


async def stream_nar(
    src: NixReader,
    dst: NixWriter,
    chunk_size: int = _CHUNK_SIZE,
) -> None:
    """Copy a NAR archive from src to dst, streaming large tokens in chunks.

    Like copy_nar but bounded memory: large file data tokens (> chunk_size)
    are read and written in chunk_size pieces instead of buffered fully.
    """
    depth: int = 0
    after_contents: bool = False

    while True:
        # Read token header (8-byte LE length prefix)
        length: int = await src.read_uint64()
        pad: int = _nar_pad(length)

        # Always write header
        dst.write_uint64(length)

        if length <= chunk_size:
            # Small token: read all at once
            data: bytes = await src.readexactly(length) if length else b""
            dst.write(data)
            if pad:
                pad_data: bytes = await src.readexactly(pad)
                dst.write(pad_data)

            if after_contents:
                after_contents = False
            else:
                try:
                    tok: str = data.decode("ascii") if data else ""
                except (UnicodeDecodeError, ValueError):
                    tok = ""
                if tok == "(":
                    depth += 1
                elif tok == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif tok == "contents":
                    after_contents = True
        else:
            # Large token: stream data in chunk_size pieces
            remaining = length
            while remaining > 0:
                to_read = min(remaining, chunk_size)
                chunk = await src.readexactly(to_read)
                dst.write(chunk)
                await dst.drain()
                remaining -= to_read
            if pad:
                pad_data = await src.readexactly(pad)
                dst.write(pad_data)
            # Large tokens are always file data (after "contents")
            after_contents = False


async def copy_nar_to_framed(
    src: NixReader,
    dst: NixWriter,
    chunk_size: int = _CHUNK_SIZE,
) -> None:
    """Read raw NAR from src, write as framed data to dst.

    Framed output: [uint64 size][data]... [uint64 0] (terminator).
    Memory bound: ~chunk_size buffered at a time.
    """
    fw = FramedWriter(dst, chunk_size)
    await stream_nar(src, fw, chunk_size)
    await fw.finalize()


async def pipe_raw_to_framed(
    src: NixReader,
    dst: NixWriter,
    size: int,
    chunk_size: int = _CHUNK_SIZE,
) -> None:
    """Read size bytes from src and write as framed data to dst.

    No NAR parsing — just reads raw bytes and wraps them in frames.
    Use this when the total byte count is known (e.g. from nar_size).
    """
    remaining = size
    while remaining > 0:
        to_read = min(remaining, chunk_size)
        chunk = await src.readexactly(to_read)
        dst.write_uint64(len(chunk))
        dst.write(chunk)
        remaining -= to_read
    dst.write_uint64(0)  # framing terminator
    await dst.drain()


async def pipe_raw_to_framed_writer(
    src: NixReader,
    fw: FramedWriter,
    size: int,
    chunk_size: int = _CHUNK_SIZE,
) -> None:
    """Read size bytes from src and write through a FramedWriter.

    Like pipe_raw_to_framed but writes into an existing FramedWriter
    (for AddMultipleToStore where multiple NARs share one framed stream).
    Does NOT call finalize — caller is responsible for that.
    """
    remaining = size
    while remaining > 0:
        to_read = min(remaining, chunk_size)
        chunk = await src.readexactly(to_read)
        fw.write(chunk)
        remaining -= to_read
