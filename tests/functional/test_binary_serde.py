"""Prototype test: Pydantic binary (de)serialization round-trip.

Creates Pydantic models for IsValidPath request/response, serializes the
original dataclass to bytes via WriteContext, deserializes to Pydantic,
re-serializes, and round-trips back.

Does NOT modify any production types.
"""

from __future__ import annotations

import asyncio
import os
import struct
from io import BytesIO
from typing import Any

import pytest
import structlog

from pynixd._binary import BinaryProtocolMessage
from pynixd.operations.is_valid_path import IsValidPathRequest, IsValidPathResponse
from pynixd.store_path import StorePath
from pynixd.types.context import ReadContext, WriteContext

# ── Pydantic mirrors of the wire types ──


class IsValidPathRequestPydantic(BinaryProtocolMessage):
    op: int
    path: str  # StorePath serializes as string on the wire


class IsValidPathResponsePydantic(BinaryProtocolMessage):
    valid: int  # nix wire uses uint64 for bool


# ── Helpers ──


class _BufWriter:
    """Minimal writer matching the NixWriter interface used by WriteContext."""

    def __init__(self, b: BytesIO) -> None:
        self._b = b
        self.identifier = "test"

    def write_uint64(self, v: int) -> None:
        self._b.write(struct.pack("<Q", v))

    def write_string(self, v: Any) -> None:
        s = str(v).encode("utf-8")
        self._b.write(struct.pack("<Q", len(s)))
        self._b.write(s)


async def _serialize_request(req: IsValidPathRequest) -> bytes:
    """Serialize the original dataclass request to bytes via WriteContext."""
    buf = BytesIO()
    ctx = WriteContext(writer=_BufWriter(buf), version=1)  # type: ignore[arg-type]
    await req.serialize(ctx)
    return buf.getvalue()


def _serialize_response(resp: IsValidPathResponse) -> bytes:
    """Serialize the original dataclass response body to bytes."""
    buf = BytesIO()
    buf.write(struct.pack("<Q", 1 if resp.valid else 0))
    return buf.getvalue()


def _bytes_to_stream_reader(data: bytes) -> asyncio.StreamReader:
    """Wrap bytes in an asyncio.StreamReader for testing."""
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


class _PipeWriter:
    """Minimal asyncio.StreamWriter backed by an os.pipe.

    Used to test BinaryProtocolMessage.to_stream() which requires
    an asyncio.StreamWriter.
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self.transport = _DummyTransport()

    def write(self, data: bytes) -> None:
        os.write(self._fd, data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        os.close(self._fd)

    async def wait_closed(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, name: str, default=None):
        return default


class _DummyTransport:
    """Minimal transport that satisfies StreamWriter.__del__."""

    def is_closing(self) -> bool:
        return True


class _BufReader:
    """Minimal reader matching the NixReader interface used by ReadContext."""

    def __init__(self, data: bytes) -> None:
        self._buf = BytesIO(data)
        self.identifier = "test"

    async def read_uint64(self) -> int:
        return struct.unpack("<Q", self._buf.read(8))[0]

    async def read_string(self, type_: type) -> Any:
        n = await self.read_uint64()
        raw = self._buf.read(n)
        return type_(raw.decode("utf-8"))

    async def read_string_set(self, type_: type) -> set:
        n = await self.read_uint64()
        result = set()
        for _ in range(n):
            result.add(await self.read_string(type_))
        return result


async def _deserialize_request_to_original(data: bytes) -> IsValidPathRequest:
    """Deserialize bytes to original IsValidPathRequest via ReadContext.

    The Pydantic model writes [op][path] but the original deserialize
    only reads [path] (op is a class variable). Skip the op field.
    """
    # Skip the first uint64 (op) to align with original deserialize
    reader = _BufReader(data)
    await reader.read_uint64()  # skip op
    ctx = ReadContext(reader=reader, version=1)  # type: ignore[arg-type]
    return await IsValidPathRequest.deserialize(ctx)  # type: ignore[return-value]


async def _deserialize_response_to_original(data: bytes) -> IsValidPathResponse:
    """Deserialize bytes to original IsValidPathResponse."""
    reader = _BufReader(data)
    resp = IsValidPathResponse.__new__(IsValidPathResponse)
    resp.logger = structlog.get_logger("test")
    resp.valid = await reader.read_uint64() != 0
    return resp


class _FakeLogs:
    """Minimal OperationLogs stub."""

    async def serialize(self, ctx: Any) -> None:
        pass

    @classmethod
    async def deserialize(cls, ctx: Any) -> _FakeLogs:  # noqa: ARG003
        return cls()


async def _pydantic_to_bytes(msg: BinaryProtocolMessage) -> bytes:
    """Serialize a Pydantic model to bytes via to_stream + pipe."""
    r_fd, w_fd = os.pipe()
    sw = _PipeWriter(w_fd)
    await msg.to_stream(sw)  # type: ignore[arg-type]
    sw.close()
    raw = os.read(r_fd, 65536)
    os.close(r_fd)
    return raw


# ── Tests ──


async def test_request_roundtrip():
    """Original → bytes → Pydantic → bytes → Pydantic."""
    # 1. Create original request
    sp = StorePath("/nix/store/kmv02c08xqj5c37mc2l53lqpklzrvypl-test")
    original = IsValidPathRequest(path=sp)

    # 2. Serialize original to bytes
    data = await _serialize_request(original)
    assert len(data) > 0

    # 3. Deserialize bytes to Pydantic model
    reader = _bytes_to_stream_reader(data)
    pydantic_req = await IsValidPathRequestPydantic.from_stream(reader)

    # 4. Verify fields match
    assert pydantic_req.op == 1  # IsValidPath op number
    assert pydantic_req.path == str(sp)

    # 5. Serialize Pydantic model back to bytes
    raw = await _pydantic_to_bytes(pydantic_req)

    # 6. Deserialize those bytes to a SECOND Pydantic instance
    reader2 = _bytes_to_stream_reader(raw)
    pydantic_req2 = await IsValidPathRequestPydantic.from_stream(reader2)

    # 7. Compare both Pydantic instances
    assert pydantic_req2.op == pydantic_req.op
    assert pydantic_req2.path == pydantic_req.path


async def test_response_roundtrip():
    """Original response → bytes → Pydantic → bytes → Pydantic."""
    # 1. Create original response
    original = IsValidPathResponse(valid=True)

    # 2. Serialize original to bytes
    data = _serialize_response(original)
    assert len(data) > 0

    # 3. Deserialize bytes to Pydantic model
    reader = _bytes_to_stream_reader(data)
    pydantic_resp = await IsValidPathResponsePydantic.from_stream(reader)

    # 4. Verify fields match
    assert pydantic_resp.valid == 1

    # 5. Serialize Pydantic model back to bytes
    raw = await _pydantic_to_bytes(pydantic_resp)

    # 6. Deserialize to second Pydantic instance
    reader2 = _bytes_to_stream_reader(raw)
    pydantic_resp2 = await IsValidPathResponsePydantic.from_stream(reader2)

    # 7. Compare
    assert pydantic_resp2.valid == pydantic_resp.valid


async def test_request_pydantic_to_original():
    """Pydantic → bytes → original IsValidPathRequest."""
    # 1. Create Pydantic model directly
    sp_str = "/nix/store/kmv02c08xqj5c37mc2l53lqpklzrvypl-test"
    pydantic_req = IsValidPathRequestPydantic(op=1, path=sp_str)

    # 2. Serialize Pydantic to bytes
    raw = await _pydantic_to_bytes(pydantic_req)
    assert len(raw) > 0

    # 3. Deserialize bytes to original dataclass
    original = await _deserialize_request_to_original(raw)

    # 4. Verify fields match
    assert str(original.path) == sp_str


async def test_response_pydantic_to_original():
    """Pydantic → bytes → original IsValidPathResponse."""
    # 1. Create Pydantic model directly
    pydantic_resp = IsValidPathResponsePydantic(valid=1)

    # 2. Serialize Pydantic to bytes
    raw = await _pydantic_to_bytes(pydantic_resp)
    assert len(raw) > 0

    # 3. Deserialize bytes to original dataclass
    original = await _deserialize_response_to_original(raw)

    # 4. Verify fields match
    assert original.valid is True
