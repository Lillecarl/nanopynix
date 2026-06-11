"""Roundtrip test: WireMessage ↔ existing dataclass."""

from __future__ import annotations

from io import BytesIO

import pytest

from pynixd._binary import WireMessage
from pynixd.derived_path import DerivedPath
from pynixd.operations.build_paths import BuildPathsRequest
from pynixd.operations.is_valid_path import (
    IsValidPathRequest,
    IsValidPathResponse,
)
from pynixd.store_path import StorePath
from pynixd.types import BuildMode
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

    def write_string_set(self, v: set) -> None:
        self.write_uint64(len(v))
        for item in v:
            self.write_string(item)


class _R:
    def __init__(self, data: bytes) -> None:
        self._b = BytesIO(data)

    async def read_uint64(self) -> int:
        return int.from_bytes(self._b.read(8), "little")

    async def read_string(self, _: type) -> str:
        n = int.from_bytes(self._b.read(8), "little")
        return self._b.read(n).decode()

    async def read_string_set(self, _: object) -> set[str]:
        n = await self.read_uint64()
        result: set[str] = set()
        for _ in range(n):
            result.add(await self.read_string(str))
        return result


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


class BuildPathsReq(WireMessage):
    derived_paths: set[str]  # set[DerivedPath] → set of strings on wire
    build_mode: int  # BuildMode → uint64 on wire


async def test_build_paths_request_roundtrip():
    dp1 = DerivedPath("/nix/store/aaa.drv!out")
    dp2 = DerivedPath("/nix/store/bbb.drv!out")
    orig = BuildPathsRequest(derived_paths={dp1, dp2}, build_mode=BuildMode.NORMAL)

    # orig → bytes
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=1))  # type: ignore[arg-type]
    data = buf.getvalue()

    # bytes → WireMessage (skip op uint64)
    r = _R(data)
    await r.read_uint64()  # skip op
    wm = await BuildPathsReq.deserialize(ReadContext(reader=r, version=1))  # type: ignore[arg-type]

    # Verify fields
    assert wm.build_mode == BuildMode.NORMAL.value  # int value of the enum
    assert len(wm.derived_paths) == 2
    assert "/nix/store/aaa.drv!out" in wm.derived_paths
    assert "/nix/store/bbb.drv!out" in wm.derived_paths

    # WireMessage → bytes
    buf2 = BytesIO()
    await wm.serialize(WriteContext(writer=_W(buf2), version=1))  # type: ignore[arg-type]
    data2 = buf2.getvalue()

    # bytes → second WireMessage
    wm2 = await BuildPathsReq.deserialize(ReadContext(reader=_R(data2), version=1))  # type: ignore[arg-type]
    assert wm2.derived_paths == wm.derived_paths
    assert wm2.build_mode == wm.build_mode
