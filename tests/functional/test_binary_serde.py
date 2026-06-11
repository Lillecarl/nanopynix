"""Roundtrip test: WireMessage ↔ existing dataclass."""

from __future__ import annotations

from io import BytesIO

import pytest

from pynixd._binary import WireMessage
from pynixd.operations.is_valid_path import (
    IsValidPathRequest,
    IsValidPathResponse,
)
from pynixd.store_path import StorePath
from pynixd.types.context import ReadContext, WriteContext


# Pydantic mirror — same fields, different base class
class Req(WireMessage):
    path: str  # StorePath → string on wire


class Resp(WireMessage):
    valid: int  # bool → uint64 on wire


# Helpers — minimal, just enough to bridge NixWriter/Reader to BytesIO
class _W:
    def __init__(self, b: BytesIO) -> None:
        self._b = b
        self.identifier = "test"

    def write_uint64(self, v: int) -> None:
        self._b.write(v.to_bytes(8, "little"))

    def write_string(self, v: object) -> None:
        encoded = str(v).encode()
        self._b.write(len(encoded).to_bytes(8, "little"))
        self._b.write(encoded)


class _R:
    def __init__(self, data: bytes) -> None:
        self._b = BytesIO(data)

    async def read_uint64(self) -> int:
        return int.from_bytes(self._b.read(8), "little")

    async def read_string(self, _: type) -> str:
        n = int.from_bytes(self._b.read(8), "little")
        return self._b.read(n).decode()


async def test_request_roundtrip():
    sp = StorePath("/nix/store/abc-test")
    orig = IsValidPathRequest(path=sp)
    # orig → bytes
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=1))  # type: ignore[arg-type]
    data = buf.getvalue()
    # bytes → WireMessage
    r = _R(data)
    await r.read_uint64()  # skip op written by original serialize
    wm = await Req.deserialize(ReadContext(reader=r, version=1))  # type: ignore[arg-type]
    assert wm.path == str(sp)
    # WireMessage → bytes
    buf2 = BytesIO()
    await wm.serialize(WriteContext(writer=_W(buf2), version=1))  # type: ignore[arg-type]
    data2 = buf2.getvalue()
    # bytes → WireMessage
    wm2 = await Req.deserialize(ReadContext(reader=_R(data2), version=1))  # type: ignore[arg-type]
    assert wm2.path == wm.path


async def test_response_roundtrip():
    orig = IsValidPathResponse(valid=True)
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=1))  # type: ignore[arg-type]
    data = buf.getvalue()
    r = _R(data)
    await r.read_uint64()  # skip STDERR_LAST from empty logs
    wm = await Resp.deserialize(ReadContext(reader=r, version=1))  # type: ignore[arg-type]
    assert wm.valid == 1
    buf2 = BytesIO()
    await wm.serialize(WriteContext(writer=_W(buf2), version=1))  # type: ignore[arg-type]
    data2 = buf2.getvalue()
    wm2 = await Resp.deserialize(ReadContext(reader=_R(data2), version=1))  # type: ignore[arg-type]
    assert wm2.valid == wm.valid
