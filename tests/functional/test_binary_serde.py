"""Roundtrip test: WireMessage ↔ existing dataclass."""

from __future__ import annotations

from io import BytesIO

import pytest

from pynixd._binary import Conditional, WireMessage, WireStorePath
from pynixd.derived_path import DerivedPath
from pynixd.operations.build_paths import BuildPathsRequest
from pynixd.operations.is_valid_path import (
    IsValidPathRequest,
    IsValidPathResponse,
)
from pynixd.operations.query_path_info import QueryPathInfoResponse
from pynixd.store_path import StorePath
from pynixd.types import BuildMode
from pynixd.types.context import ReadContext, WriteContext
from pynixd.types.path_info import UnkeyedValidPathInfo


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

    def write_bytes(self, v: bytes) -> None:
        self.write_uint64(len(v))
        self._b.write(v)


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

    async def read_bytes(self) -> bytes:
        n = await self.read_uint64()
        return self._b.read(n)


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


class PathInfo(WireMessage):
    deriver: str
    nar_hash: str
    references: set[str]
    registration_time: int
    nar_size: int
    ultimate: int
    sigs: set[str]
    ca: str


class QueryPathInfoResp(WireMessage):
    info: Conditional[PathInfo]


async def test_query_path_info_response_roundtrip():
    info = UnkeyedValidPathInfo(
        deriver=StorePath("/nix/store/deriver.drv"),
        nar_hash="sha256:abc123",
        references={StorePath("/nix/store/ref1"), StorePath("/nix/store/ref2")},
        registration_time=12345678,
        nar_size=4096,
        ultimate=1,
        sigs={"sig1", "sig2"},
        ca="fixed:r:sha256:xyz",
    )

    # Valid response: original → Pydantic
    orig = QueryPathInfoResponse(info=info)
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=1))  # type: ignore[arg-type]
    data = buf.getvalue()

    r = _R(data)
    await r.read_uint64()  # skip logs (STDERR_LAST)
    wm = await QueryPathInfoResp.deserialize(ReadContext(reader=r, version=1))  # type: ignore[arg-type]

    assert wm.info.is_present
    assert wm.info.value is not None
    assert wm.info.value.deriver == "/nix/store/deriver.drv"
    assert wm.info.value.nar_hash == "abc123"  # sha256: stripped on wire
    assert len(wm.info.value.references) == 2
    assert "/nix/store/ref1" in wm.info.value.references
    assert "/nix/store/ref2" in wm.info.value.references
    assert wm.info.value.registration_time == 12345678
    assert wm.info.value.nar_size == 4096
    assert wm.info.value.ultimate == 1
    assert len(wm.info.value.sigs) == 2
    assert "sig1" in wm.info.value.sigs
    assert wm.info.value.ca == "fixed:r:sha256:xyz"

    # WireMessage → bytes → WireMessage
    buf2 = BytesIO()
    await wm.serialize(WriteContext(writer=_W(buf2), version=1))  # type: ignore[arg-type]
    data2 = buf2.getvalue()
    wm2 = await QueryPathInfoResp.deserialize(ReadContext(reader=_R(data2), version=1))  # type: ignore[arg-type]
    assert wm2.info.is_present
    assert wm2.info.value is not None
    assert wm2.info.value.deriver == wm.info.value.deriver
    assert wm2.info.value.nar_hash == wm.info.value.nar_hash
    assert wm2.info.value.references == wm.info.value.references
    assert wm2.info.value.registration_time == wm.info.value.registration_time
    assert wm2.info.value.nar_size == wm.info.value.nar_size
    assert wm2.info.value.ultimate == wm.info.value.ultimate
    assert wm2.info.value.sigs == wm.info.value.sigs
    assert wm2.info.value.ca == wm.info.value.ca

    # Invalid response (no info): original → Pydantic
    orig2 = QueryPathInfoResponse(info=None)
    buf3 = BytesIO()
    await orig2.serialize(WriteContext(writer=_W(buf3), version=1))  # type: ignore[arg-type]
    data3 = buf3.getvalue()

    r2 = _R(data3)
    await r2.read_uint64()  # skip logs (STDERR_LAST)
    wm3 = await QueryPathInfoResp.deserialize(ReadContext(reader=r2, version=1))  # type: ignore[arg-type]
    assert not wm3.info.is_present
    assert wm3.info.value is None


class ReqWithStorePath(WireMessage):
    path: WireStorePath  # auto-detected, no register_nested_model needed


async def test_wire_store_path_roundtrip():
    # Pydantic → bytes → Pydantic
    sp = WireStorePath(path="/nix/store/abc-test")
    req = ReqWithStorePath(path=sp)

    buf = BytesIO()
    await req.serialize(WriteContext(writer=_W(buf), version=1))  # type: ignore[arg-type]
    data = buf.getvalue()

    r = _R(data)
    # No op skip needed — ReqWithStorePath has no ClassVar
    wm = await ReqWithStorePath.deserialize(ReadContext(reader=r, version=1))  # type: ignore[arg-type]

    assert str(wm.path) == str(sp)
    assert wm.path == sp
    assert isinstance(wm.path, WireStorePath)
    assert wm.path.path == "/nix/store/abc-test"

    # Pydantic → bytes → Pydantic (full roundtrip)
    buf2 = BytesIO()
    await wm.serialize(WriteContext(writer=_W(buf2), version=1))  # type: ignore[arg-type]
    wm2 = await ReqWithStorePath.deserialize(ReadContext(reader=_R(buf2.getvalue()), version=1))  # type: ignore[arg-type]

    assert wm2.path == wm.path
    assert str(wm2.path) == "/nix/store/abc-test"
