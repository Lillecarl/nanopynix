"""
Nix daemon wire protocol primitives (async).

All integers are little-endian uint64. Strings are length-prefixed with
zero-padding to 8-byte alignment.

Read functions are async (for asyncio.StreamReader / asyncssh.SSHReader).
Write functions are sync (writer.write() buffers; callers await drain()).
"""

from __future__ import annotations

import asyncio
import os
import struct
from abc import abstractmethod
from typing import Final

import asyncssh

_CHUNK_SIZE = int(os.environ.get("PYNIXD_CHUNK_SIZE", 1024 * 1024))

_SSH_WINDOW_SIZE = int(os.environ.get("PYNIXD_SSH_WINDOW", 16 * 1024 * 1024))


def _nar_pad(n: int) -> int:
    """Return number of padding bytes needed for 8-byte alignment."""
    return (8 - n % 8) % 8


# ── NixReader / NixWriter ──────────────────────────────────────


class NixReader:
    """Wraps AsyncReader with wire protocol methods and dirty checking."""

    @abstractmethod
    async def readexactly(self, n: int) -> bytes:
        """Read exactly N bytes"""
        raise NotImplementedError

    async def read_uint64(self) -> int:
        return struct.unpack("<Q", await self.readexactly(8))[0]

    async def read_bool(self) -> bool:
        return await self.read_uint64() != 0

    async def read_optional_uint64(self) -> int | None:
        tag = await self.read_uint64()
        if tag == 0:
            return None
        return await self.read_uint64()

    async def read_bytes(self) -> bytes:
        length: int = await self.read_uint64()
        data: bytes = await self.readexactly(length)
        pad: int = _nar_pad(length)
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

    @abstractmethod
    async def is_dirty(self) -> bool:
        """Check if reader has unread buffered data."""
        raise NotImplementedError


_SSH_READ_AHEAD = 16 * 1024  # read-ahead size to amortize asyncssh lock overhead


class SSHNixReader(NixReader):
    def __init__(self, reader: asyncssh.SSHReader) -> None:
        self.reader = reader
        self._buf = bytearray()

    async def readexactly(self, n: int) -> bytes:
        # Fast path: serve entirely from local buffer
        if len(self._buf) >= n:
            result = bytes(self._buf[:n])
            del self._buf[:n]
            return result

        # Need more data from SSH channel
        needed = n - len(self._buf)
        if needed <= _SSH_READ_AHEAD:
            # Small read: try to grab extra data to fill future reads
            try:
                data = await self.reader.read(max(needed, _SSH_READ_AHEAD))
            except asyncssh.misc.ConnectionLost:
                raise EOFError("SSH connection lost")
            if not data:
                raise EOFError("SSH channel closed")
            self._buf.extend(data)
            # May still not have enough if channel had less available
            while len(self._buf) < n:
                data = await self.reader.readexactly(n - len(self._buf))
                self._buf.extend(data)
            result = bytes(self._buf[:n])
            del self._buf[:n]
            return result
        else:
            # Large read (NAR data etc.): read exactly what's needed
            data = await self.reader.readexactly(needed)
            if self._buf:
                self._buf.extend(data)
                result = bytes(self._buf[:n])
                del self._buf[:n]
                return result
            return data

    async def is_dirty(self) -> bool:
        if self._buf:
            return True
        if hasattr(self.reader, "get_read_buffer_size"):
            return self.reader.get_read_buffer_size() > 0  # type: ignore[reportAttributeAccessIssue]
        # Fallback to private access if API changed or is missing
        # _recv_buf is a dict of list of bytes
        buf = self.reader._session._recv_buf.get(  # type: ignore[attr-defined]
            self.reader._datatype,
            [],
        )
        for chunk in buf:
            if isinstance(chunk, (bytes, bytearray)) and len(chunk) > 0:
                return True
        return False


_UNIX_READ_AHEAD = 16 * 1024  # read-ahead size to amortize syscall overhead


class UnixNixReader(NixReader):
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self._buf = bytearray()

    async def readexactly(self, n: int) -> bytes:
        # Fast path: serve entirely from local buffer
        if len(self._buf) >= n:
            result = bytes(self._buf[:n])
            del self._buf[:n]
            return result

        # Need more data from the stream
        needed = n - len(self._buf)
        if needed <= _UNIX_READ_AHEAD:
            # Small read: try to grab extra data to fill future reads
            data = await self.reader.read(max(needed, _UNIX_READ_AHEAD))
            if not data:
                raise EOFError("Unix stream closed")
            self._buf.extend(data)
            # May still not have enough if stream had less available
            while len(self._buf) < n:
                data = await self.reader.readexactly(n - len(self._buf))
                self._buf.extend(data)
            result = bytes(self._buf[:n])
            del self._buf[:n]
            return result
        else:
            # Large read (NAR data etc.): read exactly what's needed
            data = await self.reader.readexactly(needed)
            if self._buf:
                self._buf.extend(data)
                result = bytes(self._buf[:n])
                del self._buf[:n]
                return result
            return data

    async def is_dirty(self) -> bool:
        if self._buf:
            return True
        # Check internal buffer without consuming data
        return len(self.reader._buffer) > 0  # type: ignore[attr-defined]


class NixWriter:
    """Wraps AsyncWriter with wire protocol methods."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write raw bytes."""
        raise NotImplementedError

    @abstractmethod
    async def drain(self) -> None:
        """Flush writer."""
        raise NotImplementedError

    @abstractmethod
    async def is_dirty(self) -> bool:
        """Check if writer has un-drained data."""
        raise NotImplementedError

    async def close(self) -> None:
        """Close the underlying transport. Override in subclasses."""

    def write_uint64(self, val: int) -> None:
        self.write(struct.pack("<Q", val))

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

    def write_string_list(self, items: list[str]) -> None:
        self.write_uint64(len(items))
        for item in items:
            self.write_string(item)

    def write_string_set(self, items: set[str]) -> None:
        self.write_uint64(len(items))
        for item in items:
            self.write_string(item)

    def framed(self, chunk_size: int = _CHUNK_SIZE) -> FramedWriter:
        """Create a FramedWriter that writes framed data to this writer."""
        return FramedWriter(self, chunk_size)


class SSHNixWriter(NixWriter):
    def __init__(self, writer: asyncssh.SSHWriter) -> None:
        self.writer = writer
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        if len(data) >= _SSH_READ_AHEAD:
            # Large data (NAR chunks): flush pending small writes,
            # then pass directly to asyncssh — avoids copying MBs
            # through the bytearray
            if self._buf:
                self.writer.write(bytes(self._buf))
                self._buf.clear()
            self.writer.write(data)
        else:
            self._buf.extend(data)

    async def drain(self) -> None:
        if self._buf:
            self.writer.write(bytes(self._buf))
            self._buf.clear()
        await self.writer.drain()

    async def is_dirty(self) -> bool:
        if self._buf:
            return True
        return self.writer._channel.get_write_buffer_size() > 0  # type: ignore[reportAttributeAccessIssue]

    async def close(self) -> None:
        if self._buf:
            self.writer.write(bytes(self._buf))
            self._buf.clear()
        self.writer.close()


class UnixNixWriter(NixWriter):
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        if len(data) >= _UNIX_READ_AHEAD:
            # Large data (NAR chunks): flush pending small writes,
            # then pass directly to stream — avoids copying MBs
            # through the bytearray
            if self._buf:
                self.writer.write(bytes(self._buf))
                self._buf.clear()
            self.writer.write(data)
        else:
            self._buf.extend(data)

    async def drain(self) -> None:
        if self._buf:
            self.writer.write(bytes(self._buf))
            self._buf.clear()
        await self.writer.drain()

    async def is_dirty(self) -> bool:
        if self._buf:
            return True
        return self.writer.transport.get_write_buffer_size() > 0

    async def close(self) -> None:
        if self._buf:
            self.writer.write(bytes(self._buf))
            self._buf.clear()
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
        self._src = src
        self._buf = bytearray()
        self._eof = False

    async def _fill(self, needed: int) -> None:
        """Read framed chunks until buffer has at least `needed` bytes."""
        while len(self._buf) < needed:
            if self._eof:
                raise EOFError(
                    f"Framed stream ended, need {needed} bytes, have {len(self._buf)}"
                )
            size = await self._src.read_uint64()
            if size == 0:
                self._eof = True
                if len(self._buf) < needed:
                    raise EOFError(
                        f"Framed stream ended, need {needed} bytes, "
                        f"have {len(self._buf)}"
                    )
                return
            data = await self._src.readexactly(size)
            self._buf.extend(data)

    async def readexactly(self, n: int) -> bytes:
        await self._fill(n)
        result = bytes(self._buf[:n])
        self._buf = self._buf[n:]
        return result

    async def is_dirty(self) -> bool:
        if self._buf:
            return True
        return await self._src.is_dirty()

    @property
    def at_eof(self) -> bool:
        return self._eof and len(self._buf) == 0

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
        self._dst = dst
        self._buf = bytearray()
        self._chunk_size = chunk_size

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        while len(self._buf) >= self._chunk_size:
            self._dst.write_uint64(self._chunk_size)
            self._dst.write(bytes(self._buf[: self._chunk_size]))
            self._buf = self._buf[self._chunk_size :]

    async def drain(self) -> None:
        """No-op — actual flushing happens in write() and finalize()."""

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
