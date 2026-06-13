"""Roundtrip test: WireMessage ↔ existing dataclass."""

from __future__ import annotations

import json as json_lib
from io import BytesIO

import pytest

from pynixd.constants import PROTOCOL_VERSION, proto
from pynixd.derived_path import DerivedPath
from pynixd.operations.build_paths import BuildPathsRequest
from pynixd.operations.is_valid_path import (
    IsValidPathRequest,
    IsValidPathResponse,
)
from pynixd.operations.query_path_info import QueryPathInfoResponse
from pynixd.serde import (
    WireBuildDerivationResponse,
    WireBuildResult,
    WireDrvOutput,
    WireMessage,
    WireNARHash,
    WireOptMicroseconds,
    WireQueryPathInfoResponse,
    WireRealisation,
    WireSignature,
    WireStorePath,
    WireTime,
    WireUnkeyedValidPathInfo,
)
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

    def write_bool(self, v: bool) -> None:
        self._b.write((1 if v else 0).to_bytes(8, "little"))

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

    def write_dict(self, v: dict) -> None:
        self.write_uint64(len(v))
        for k, val in v.items():
            self.write_string(k)
            self.write_string(val)


class _R:
    def __init__(self, data: bytes) -> None:
        self._b = BytesIO(data)

    async def read_uint64(self) -> int:
        return int.from_bytes(self._b.read(8), "little")

    async def read_bool(self) -> bool:
        return bool(await self.read_uint64())

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

    async def read_dict(self) -> dict[str, str]:
        n = await self.read_uint64()
        result = {}
        for _ in range(n):
            k = await self.read_string(str)
            v = await self.read_string(str)
            result[k] = v
        return result


async def test_request_roundtrip():
    sp = StorePath("/nix/store/abc-test")
    orig = IsValidPathRequest(path=sp)
    # orig → bytes
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()
    # bytes → WireMessage
    r = _R(data)
    await r.read_uint64()  # skip op written by original serialize
    wm = await Req.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm.path == str(sp)
    # WireMessage → bytes
    buf2 = BytesIO()
    await wm.to_writer(WriteContext(writer=_W(buf2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data2 = buf2.getvalue()
    # bytes → WireMessage
    wm2 = await Req.from_reader(ReadContext(reader=_R(data2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm2.path == wm.path


async def test_response_roundtrip():
    orig = IsValidPathResponse(valid=True)
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()
    r = _R(data)
    await r.read_uint64()  # skip STDERR_LAST from empty logs
    wm = await Resp.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm.valid == 1
    buf2 = BytesIO()
    await wm.to_writer(WriteContext(writer=_W(buf2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data2 = buf2.getvalue()
    wm2 = await Resp.from_reader(ReadContext(reader=_R(data2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm2.valid == wm.valid


class BuildPathsReq(WireMessage):
    derived_paths: set[str]  # set[DerivedPath] → set of strings on wire
    build_mode: BuildMode  # IntEnum → uint64 on wire


async def test_build_paths_request_roundtrip():
    dp1 = DerivedPath("/nix/store/aaa.drv!out")
    dp2 = DerivedPath("/nix/store/bbb.drv!out")
    orig = BuildPathsRequest(derived_paths={dp1, dp2}, build_mode=BuildMode.NORMAL)

    # orig → bytes
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()

    # bytes → WireMessage (skip op uint64)
    r = _R(data)
    await r.read_uint64()  # skip op
    wm = await BuildPathsReq.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))  # type: ignore[arg-type]

    # Verify fields
    assert wm.build_mode == BuildMode.NORMAL
    assert len(wm.derived_paths) == 2
    assert "/nix/store/aaa.drv!out" in wm.derived_paths
    assert "/nix/store/bbb.drv!out" in wm.derived_paths

    # WireMessage → bytes
    buf2 = BytesIO()
    await wm.to_writer(WriteContext(writer=_W(buf2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data2 = buf2.getvalue()

    # bytes → second WireMessage
    wm2 = await BuildPathsReq.from_reader(ReadContext(reader=_R(data2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
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
    await orig.serialize(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()

    r = _R(data)
    await r.read_uint64()  # skip logs (STDERR_LAST)
    wm = await WireQueryPathInfoResponse.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))  # type: ignore[arg-type]

    assert wm.valid
    assert wm.info is not None
    assert isinstance(wm.info, WireUnkeyedValidPathInfo)
    assert str(wm.info.deriver) == "/nix/store/deriver.drv"
    assert str(wm.info.nar_hash) == "abc123"  # sha256: stripped on wire
    assert len(wm.info.references) == 2
    assert WireStorePath(path="/nix/store/ref1") in wm.info.references
    assert WireStorePath(path="/nix/store/ref2") in wm.info.references
    assert wm.info.registration_time == WireTime(ts=12345678)
    assert wm.info.nar_size == 4096
    assert wm.info.ultimate == 1
    assert len(wm.info.sigs) == 2
    # Original writes "sig1" (no colon); WireSignature reads as name="sig1", signature=""
    assert WireSignature(name="sig1", signature="") in wm.info.sigs
    assert WireSignature(name="sig2", signature="") in wm.info.sigs
    assert wm.info.ca == "fixed:r:sha256:xyz"

    # WireMessage → bytes → WireMessage
    buf2 = BytesIO()
    await wm.to_writer(WriteContext(writer=_W(buf2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data2 = buf2.getvalue()
    wm2 = await WireQueryPathInfoResponse.from_reader(ReadContext(reader=_R(data2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm2.valid
    assert wm2.info is not None
    assert isinstance(wm2.info, WireUnkeyedValidPathInfo)
    assert wm2.info.deriver == wm.info.deriver
    assert wm2.info.nar_hash == wm.info.nar_hash
    assert wm2.info.references == wm.info.references
    assert wm2.info.registration_time == wm.info.registration_time
    assert wm2.info.nar_size == wm.info.nar_size
    assert wm2.info.ultimate == wm.info.ultimate
    assert wm2.info.sigs == wm.info.sigs
    assert wm2.info.ca == wm.info.ca

    # Invalid response (no info): original → Pydantic
    orig2 = QueryPathInfoResponse(info=None)
    buf3 = BytesIO()
    await orig2.serialize(WriteContext(writer=_W(buf3), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data3 = buf3.getvalue()

    r2 = _R(data3)
    await r2.read_uint64()  # skip logs (STDERR_LAST)
    wm3 = await WireQueryPathInfoResponse.from_reader(ReadContext(reader=r2, version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert not wm3.valid
    assert wm3.info is None


class ReqWithStorePath(WireMessage):
    path: WireStorePath  # auto-detected, no register_nested_model needed


async def test_wire_store_path_roundtrip():
    # Pydantic → bytes → Pydantic
    sp = WireStorePath(path="/nix/store/abc-test")
    req = ReqWithStorePath(path=sp)

    buf = BytesIO()
    await req.to_writer(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()

    r = _R(data)
    # No op skip needed — ReqWithStorePath has no ClassVar
    wm = await ReqWithStorePath.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))  # type: ignore[arg-type]

    assert str(wm.path) == str(sp)
    assert wm.path == sp
    assert isinstance(wm.path, WireStorePath)
    assert str(wm.path) == "/nix/store/abc-test"

    # Pydantic → bytes → Pydantic (full roundtrip)
    buf2 = BytesIO()
    await wm.to_writer(WriteContext(writer=_W(buf2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    wm2 = await ReqWithStorePath.from_reader(ReadContext(reader=_R(buf2.getvalue()), version=PROTOCOL_VERSION))  # type: ignore[arg-type]

    assert wm2.path == wm.path
    assert str(wm2.path) == "/nix/store/abc-test"


async def test_wire_build_result_roundtrip():
    """Roundtrip WireBuildResult at protocol 1.38 (all fields present)."""
    br = WireBuildResult(
        status=0,
        error_msg="",
        times_built=1,
        is_non_deterministic=0,
        start_time=1000000,
        stop_time=1000500,
        built_outputs={"sha256:abc!out": '{"outPath":"/nix/store/xxx-foo"}'},
    )
    br.cpu_user = WireOptMicroseconds(tag=1, value=50000)
    br.cpu_system = WireOptMicroseconds(tag=1, value=10000)

    # Wire → bytes
    buf = BytesIO()
    await br.to_writer(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()

    # bytes → Wire (same version)
    wm = await WireBuildResult.from_reader(ReadContext(reader=_R(data), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm.status == 0
    assert wm.times_built == 1
    assert wm.start_time == 1000000
    assert wm.stop_time == 1000500
    assert wm.built_outputs == {"sha256:abc!out": '{"outPath":"/nix/store/xxx-foo"}'}
    assert wm.cpu_user.tag == 1
    assert wm.cpu_user.value == 50000
    assert wm.cpu_system.tag == 1
    assert wm.cpu_system.value == 10000

    # Full roundtrip
    buf2 = BytesIO()
    await wm.to_writer(WriteContext(writer=_W(buf2), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    wm2 = await WireBuildResult.from_reader(ReadContext(reader=_R(buf2.getvalue()), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm2.times_built == wm.times_built
    assert wm2.start_time == wm.start_time
    assert wm2.cpu_user == wm.cpu_user


async def test_wire_build_result_version_27():
    """Deserialize at protocol 1.27 — only status + error_msg survive."""
    br = WireBuildResult(status=0, error_msg="test error")
    buf = BytesIO()
    await br.to_writer(WriteContext(writer=_W(buf), version=proto(1, 27)))  # type: ignore[arg-type]
    data = buf.getvalue()
    wm = await WireBuildResult.from_reader(ReadContext(reader=_R(data), version=proto(1, 27)))  # type: ignore[arg-type]
    assert wm.status == 0
    assert wm.error_msg == "test error"
    # Version 1.27: no fields past status+error_msg
    assert wm.times_built is None
    assert wm.start_time is None
    assert wm.built_outputs is None
    assert wm.cpu_user.tag == 0
    assert wm.cpu_system.tag == 0


async def test_wire_build_derivation_response_roundtrip():
    """Roundtrip WireBuildDerivationResponse containing WireBuildResult."""
    br = WireBuildResult(
        status=0,
        error_msg="",
        times_built=1,
        is_non_deterministic=0,
        start_time=100,
        stop_time=200,
        built_outputs={"out": "/nix/store/xxx-foo"},
    )
    br.cpu_user = WireOptMicroseconds(tag=1, value=50000)
    resp = WireBuildDerivationResponse(result=br)

    buf = BytesIO()
    await resp.to_writer(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()

    wm = await WireBuildDerivationResponse.from_reader(ReadContext(reader=_R(data), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm.result.status == 0
    assert wm.result.times_built == 1
    assert wm.result.cpu_user.tag == 1
    assert wm.result.cpu_user.value == 50000


async def test_wire_build_result_json_roundtrip():
    """WireBuildResult JSON roundtrip (exercises wire_conditional + dict + primitives)."""
    br = WireBuildResult(
        status=0,
        error_msg="",
        times_built=1,
        is_non_deterministic=0,
        start_time=1000000,
        stop_time=1000500,
        built_outputs={"sha256:abc!out": '{"outPath":"/nix/store/xxx-foo"}'},
    )
    br.cpu_user = WireOptMicroseconds(tag=1, value=50000)
    br.cpu_system = WireOptMicroseconds(tag=0, value=None)

    # to_json
    data = br.to_json()
    assert '"status":0' in data
    assert '"error_msg":""' in data
    assert '"times_built":1' in data
    assert '"start_time":1000000' in data
    assert '"built_outputs":' in data
    assert '"cpu_user":{"tag":1,"value":50000}' in data
    assert '"cpu_system":{"tag":0,"value":null}' in data

    # from_json
    br2 = WireBuildResult.from_json(data)
    assert isinstance(br2, WireBuildResult)
    assert br2.status == 0
    assert br2.times_built == 1
    assert br2.start_time == 1000000
    assert br2.built_outputs == {"sha256:abc!out": '{"outPath":"/nix/store/xxx-foo"}'}
    assert br2.cpu_user.tag == 1
    assert br2.cpu_user.value == 50000
    assert br2.cpu_system.tag == 0


async def test_wire_store_path_json():
    """WireStorePath serdes as plain string in JSON."""
    sp = WireStorePath(path="/nix/store/abc-test")

    class Req(WireMessage):
        path: WireStorePath

    req = Req(path=sp)

    data = req.to_json()
    # WireStorePath serializes as plain string
    assert data == '{"path":"/nix/store/abc-test"}'

    # from_json back to Req
    req2 = Req.from_json(data)
    assert isinstance(req2.path, WireStorePath)  # pyright: ignore[reportAttributeAccessIssue]
    assert str(req2.path) == "/nix/store/abc-test"  # pyright: ignore[reportAttributeAccessIssue]
    assert req2.path == sp  # pyright: ignore[reportAttributeAccessIssue]


async def test_wire_build_result_json_null_conditional():
    """WireOptMicroseconds not present → null in JSON."""
    br = WireBuildResult(status=0, error_msg="")
    # cpu_user is WireOptMicroseconds(tag=0) by default
    assert br.cpu_user.tag == 0

    json_str = br.to_json()
    assert '"cpu_user":{"tag":0,"value":null}' in json_str
    assert '"cpu_system":{"tag":0,"value":null}' in json_str

    wm = WireBuildResult.from_json(json_str)
    assert wm.cpu_user.tag == 0  # pyright: ignore[reportAttributeAccessIssue]
    assert wm.cpu_system.tag == 0  # pyright: ignore[reportAttributeAccessIssue]


async def test_wire_depends_on_exclude_unset_valid():
    """When valid=True, info IS set from wire — appears in JSON even with exclude_unset."""
    info = UnkeyedValidPathInfo(
        deriver=StorePath("/nix/store/deriver.drv"),
        nar_hash="sha256:abc123",
        references={StorePath("/nix/store/ref1")},
    )
    orig = QueryPathInfoResponse(info=info)
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()
    r = _R(data)
    await r.read_uint64()  # skip logs
    wm = await WireQueryPathInfoResponse.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))  # type: ignore[arg-type]

    assert wm.valid is True
    json_str = wm.to_json(exclude_unset=True)
    assert '"valid":true' in json_str
    assert '"info":' in json_str  # info was read from wire → set field


async def test_wire_depends_on_exclude_unset_invalid():
    """When valid=False, info is skipped by wire_depends_on — absent from JSON with exclude_unset."""
    orig = QueryPathInfoResponse(info=None)
    buf = BytesIO()
    await orig.serialize(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()
    r = _R(data)
    await r.read_uint64()  # skip logs
    wm = await WireQueryPathInfoResponse.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))  # type: ignore[arg-type]

    assert wm.valid is False
    json_str = wm.to_json(exclude_unset=True)
    assert '"valid":false' in json_str
    assert '"info"' not in json_str  # info never read from wire → unset field


async def test_wire_version_exclude_unset():
    """Version-skipped fields don't appear in JSON with exclude_unset."""
    # Serialize at 1.38 — first serialize a full result so all fields are written
    # (version-gated fields can't be None at their required wire version)
    br = WireBuildResult(
        status=0,
        error_msg="ok",
        times_built=1,
        is_non_deterministic=0,
        start_time=100,
        stop_time=200,
        built_outputs={},
    )
    buf = BytesIO()
    await br.to_writer(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()
    # Deserialize at version 1.27 — all version-gated fields are skipped
    wm = await WireBuildResult.from_reader(ReadContext(reader=_R(data), version=proto(1, 27)))  # type: ignore[arg-type]
    assert wm.status == 0

    # At 1.27, only status+error_msg on the wire → only those are "set"
    json_str = wm.to_json(exclude_unset=True)
    assert '"status":0' in json_str
    assert '"error_msg":"ok"' in json_str
    assert '"times_built"' not in json_str  # version-skipped
    assert '"start_time"' not in json_str  # version-skipped
    assert '"cpu_user"' not in json_str  # version-skipped
    assert '"built_outputs"' not in json_str  # version-skipped


async def test_wire_realisation_roundtrip():
    """Roundtrip WireRealisation — JSON blob with proper Pydantic fields."""
    r = WireRealisation(
        id=WireDrvOutput(drvHash="sha256:abc", outputName="out"),  # pyright: ignore[reportCallIssue]
        outPath=WireStorePath(path="/nix/store/foo"),  # type: ignore[arg-type]
        signatures=["sig1", "sig2"],
        dependentRealisations={"sha256:xyz!out": "/nix/store/bar"},  # pyright: ignore[reportCallIssue]
    )
    assert r.out_path == WireStorePath(path="/nix/store/foo")  # pyright: ignore[reportAttributeAccessIssue]
    assert r.id == WireDrvOutput(drvHash="sha256:abc", outputName="out")  # pyright: ignore[reportCallIssue, reportAttributeAccessIssue]
    assert r.signatures == ["sig1", "sig2"]

    # Wire roundtrip
    buf = BytesIO()
    await r.to_writer(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()
    wm = await WireRealisation.from_reader(ReadContext(reader=_R(data), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm.out_path == r.out_path  # pyright: ignore[reportAttributeAccessIssue]
    assert wm.id == r.id  # pyright: ignore[reportAttributeAccessIssue]

    # JSON roundtrip — camelCase keys (Nix wire format)
    json_str = r.to_json()
    parsed = json_lib.loads(json_str)
    assert parsed["outPath"] == "/nix/store/foo"
    assert parsed["id"]["drvHash"] == "sha256:abc"
    assert parsed["id"]["outputName"] == "out"


async def test_wire_signature_roundtrip():
    """Roundtrip WireSignature — WireString with name/signature properties."""
    sig = WireSignature(name="cache.nixos.org-1", signature="abc123def456")
    assert sig.name == "cache.nixos.org-1"
    assert sig.signature == "abc123def456"
    assert str(sig) == "cache.nixos.org-1:abc123def456"

    # Wire roundtrip
    buf = BytesIO()
    await sig.to_writer(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    data = buf.getvalue()
    wm = await WireSignature.from_reader(ReadContext(reader=_R(data), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm.name == "cache.nixos.org-1"
    assert wm.signature == "abc123def456"
    assert str(wm) == "cache.nixos.org-1:abc123def456"

    # JSON roundtrip
    json_str = sig.to_json()
    assert json_str == '"cache.nixos.org-1:abc123def456"'


class TypedCollections(WireMessage):
    paths: set[WireStorePath]
    mapping: dict[str, WireStorePath]


async def test_typed_collections_roundtrip():
    """Roundtrip typed collections — generics handled by _find_reader/_write_value."""
    m = TypedCollections(
        paths={WireStorePath(path="/nix/store/a"), WireStorePath(path="/nix/store/b")},  # pyright: ignore[reportUnhashable]
        mapping={"key1": WireStorePath(path="/nix/store/x")},
    )
    buf = BytesIO()
    await m.to_writer(WriteContext(writer=_W(buf), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    wm = await TypedCollections.from_reader(ReadContext(reader=_R(buf.getvalue()), version=PROTOCOL_VERSION))  # type: ignore[arg-type]
    assert wm.paths == m.paths
    assert wm.mapping == m.mapping
