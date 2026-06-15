"""Binary wire roundtrip and cross-serialization tests."""

from __future__ import annotations

import json as json_lib

import pytest

from pynixd.constants import PROTOCOL_VERSION, proto
from pynixd.derived_path import DerivedPath
from pynixd.operations.build_derivation import (
    BuildDerivationRequest as OldBuildDerivationRequest,
)
from pynixd.operations.build_paths import (
    BuildPathsRequest as OldBuildPathsRequest,
)
from pynixd.operations.build_paths import (
    BuildPathsResponse as OldBuildPathsResponse,
)
from pynixd.operations.is_valid_path import (
    IsValidPathRequest,
    IsValidPathResponse,
)
from pynixd.operations.query_path_info import QueryPathInfoResponse
from pynixd.operations.query_referrers import (
    QueryReferrersRequest as OldQueryReferrersRequest,
)
from pynixd.operations.query_referrers import (
    QueryReferrersResponse as OldQueryReferrersResponse,
)
from pynixd.serde import (
    BasicDerivation as SerdeBasicDerivation,
)
from pynixd.serde import (
    BuildDerivationRequest as SerdeBuildDerivationRequest,
)
from pynixd.serde import (
    BuildDerivationResponse,
    BuildResult,
    DrvOutput,
    NARHash,
    OptMicroseconds,
    Realisation,
    Signature,
    Time,
    WireModel,
)
from pynixd.serde import (
    BuildPathsRequest as SerdeBuildPathsRequest,
)
from pynixd.serde import (
    BuildPathsResponse as SerdeBuildPathsResponse,
)
from pynixd.serde import (
    BuildResult as SerdeBuildResult,
)
from pynixd.serde import (
    DerivationOutput as SerdeDerivationOutput,
)
from pynixd.serde import (
    DerivedPath as SerdeDerivedPath,
)
from pynixd.serde import (
    IsValidPathRequest as SerdeIsValidPathRequest,
)
from pynixd.serde import (
    IsValidPathResponse as SerdeIsValidPathResponse,
)
from pynixd.serde import (
    QueryPathInfoResponse as SerdeQueryPathInfoResponse,
)
from pynixd.serde import (
    QueryReferrersRequest as SerdeQueryReferrersRequest,
)
from pynixd.serde import (
    QueryReferrersResponse as SerdeQueryReferrersResponse,
)
from pynixd.serde import (
    StorePath as SerdeStorePath,
)
from pynixd.serde import (
    UnkeyedValidPathInfo as SerdeUnkeyedValidPathInfo,
)
from pynixd.store_path import StorePath
from pynixd.types import (
    BasicDerivation as OldBasicDerivation,
)
from pynixd.types import (
    BuildMode,
    BuildResultStatus,
)
from pynixd.types import (
    BuildResult as OldBuildResult,
)
from pynixd.types import (
    DerivationOutput as OldDerivationOutput,
)
from pynixd.types.context import ReadContext, WriteContext
from pynixd.types.path_info import UnkeyedValidPathInfo
from pynixd.wire import BytesReader, BytesWriter

from .conftest import read_ctx


class PathInfo(WireModel):
    deriver: str
    nar_hash: str
    references: set[str]
    registration_time: int
    nar_size: int
    ultimate: int
    sigs: set[str]
    ca: str


class ReqWithStorePath(WireModel):
    path: SerdeStorePath  # auto-detected, no register_nested_model needed


class TypedCollections(WireModel):
    paths: set[SerdeStorePath]
    mapping: dict[str, SerdeStorePath]


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
    w = BytesWriter()
    await orig.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()

    r = BytesReader(data)
    wm = await SerdeQueryPathInfoResponse.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    assert wm.valid
    assert wm.info is not None
    assert isinstance(wm.info, SerdeUnkeyedValidPathInfo)
    assert str(wm.info.deriver) == "/nix/store/deriver.drv"
    assert str(wm.info.nar_hash) == "abc123"  # sha256: stripped on wire
    assert len(wm.info.references) == 2
    assert SerdeStorePath(path="/nix/store/ref1") in wm.info.references
    assert SerdeStorePath(path="/nix/store/ref2") in wm.info.references
    assert wm.info.registration_time == Time(ts=12345678)
    assert wm.info.nar_size == 4096
    assert wm.info.ultimate is True
    assert len(wm.info.sigs) == 2
    # Original writes "sig1" (no colon); Signature reads as name="sig1", signature=""
    assert Signature(name="sig1", signature="") in wm.info.sigs
    assert Signature(name="sig2", signature="") in wm.info.sigs
    assert wm.info.ca == "fixed:r:sha256:xyz"

    # WireModel → bytes → WireModel
    w2 = BytesWriter()
    await wm.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    data2 = w2.get_bytes()
    wm2 = await SerdeQueryPathInfoResponse.from_reader(ReadContext(reader=BytesReader(data2), version=PROTOCOL_VERSION))
    assert wm2.valid
    assert wm2.info is not None
    assert isinstance(wm2.info, SerdeUnkeyedValidPathInfo)
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
    w3 = BytesWriter()
    await orig2.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()

    r2 = BytesReader(data3)
    wm3 = await SerdeQueryPathInfoResponse.from_reader(ReadContext(reader=r2, version=PROTOCOL_VERSION))
    assert not wm3.valid
    assert wm3.info is None


async def test_wire_store_path_roundtrip():
    # Pydantic → bytes → Pydantic
    sp = SerdeStorePath(path="/nix/store/abc-test")
    req = ReqWithStorePath(path=sp)

    w = BytesWriter()
    await req.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()

    r = BytesReader(data)
    # No op skip needed — ReqWithStorePath has no ClassVar
    wm = await ReqWithStorePath.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    assert str(wm.path) == str(sp)
    assert wm.path == sp
    assert isinstance(wm.path, SerdeStorePath)
    assert str(wm.path) == "/nix/store/abc-test"

    # Pydantic → bytes → Pydantic (full roundtrip)
    w2 = BytesWriter()
    await wm.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    wm2 = await ReqWithStorePath.from_reader(ReadContext(reader=BytesReader(w2.get_bytes()), version=PROTOCOL_VERSION))

    assert wm2.path == wm.path
    assert str(wm2.path) == "/nix/store/abc-test"


async def test_wire_build_result_roundtrip():
    """Roundtrip BuildResult at protocol 1.38 (all fields present)."""
    br = BuildResult(
        status=0,
        error_msg="",
        times_built=1,
        is_non_deterministic=0,
        start_time=1000000,
        stop_time=1000500,
        built_outputs={"sha256:abc!out": '{"outPath":"/nix/store/xxx-foo"}'},
    )
    br.cpu_user = OptMicroseconds(tag=1, value=50000)
    br.cpu_system = OptMicroseconds(tag=1, value=10000)

    # Wire → bytes
    w = BytesWriter()
    await br.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()

    # bytes → Wire (same version)
    wm = await BuildResult.from_reader(ReadContext(reader=BytesReader(data), version=PROTOCOL_VERSION))
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
    w2 = BytesWriter()
    await wm.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    wm2 = await BuildResult.from_reader(ReadContext(reader=BytesReader(w2.get_bytes()), version=PROTOCOL_VERSION))
    assert wm2.times_built == wm.times_built
    assert wm2.start_time == wm.start_time
    assert wm2.cpu_user == wm.cpu_user


async def test_wire_build_result_version_27():
    """Deserialize at protocol 1.27 — only status + error_msg survive."""
    br = BuildResult(status=0, error_msg="test error")
    w = BytesWriter()
    await br.to_writer(WriteContext(writer=w, version=proto(1, 27)))
    data = w.get_bytes()
    wm = await BuildResult.from_reader(ReadContext(reader=BytesReader(data), version=proto(1, 27)))
    assert wm.status == 0
    assert wm.error_msg == "test error"
    # Version 1.27: no fields past status+error_msg
    assert wm.times_built is None
    assert wm.start_time is None
    assert wm.built_outputs is None
    assert wm.cpu_user.tag == 0
    assert wm.cpu_system.tag == 0


async def test_wire_build_derivation_response_roundtrip():
    """Roundtrip BuildDerivationResponse containing BuildResult."""
    br = BuildResult(
        status=0,
        error_msg="",
        times_built=1,
        is_non_deterministic=0,
        start_time=100,
        stop_time=200,
        built_outputs={"out": "/nix/store/xxx-foo"},
    )
    br.cpu_user = OptMicroseconds(tag=1, value=50000)
    resp = BuildDerivationResponse(result=br)

    w = BytesWriter()
    await resp.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()

    wm = await BuildDerivationResponse.from_reader(ReadContext(reader=BytesReader(data), version=PROTOCOL_VERSION))
    assert wm.result.status == 0
    assert wm.result.times_built == 1
    assert wm.result.cpu_user.tag == 1
    assert wm.result.cpu_user.value == 50000


async def test_wire_depends_on_exclude_unset_valid():
    """When valid=True, info IS set from wire — appears in JSON even with exclude_unset."""
    info = UnkeyedValidPathInfo(
        deriver=StorePath("/nix/store/deriver.drv"),
        nar_hash="sha256:abc123",
        references={StorePath("/nix/store/ref1")},
    )
    orig = QueryPathInfoResponse(info=info)
    w = BytesWriter()
    await orig.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    wm = await SerdeQueryPathInfoResponse.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    assert wm.valid is True
    json_str = wm.to_json(exclude_unset=True)
    assert '"valid":true' in json_str
    assert '"info":' in json_str  # info was read from wire → set field


async def test_wire_depends_on_exclude_unset_invalid():
    """When valid=False, info is skipped by wire_depends_on — absent from JSON with exclude_unset."""
    orig = QueryPathInfoResponse(info=None)
    w = BytesWriter()
    await orig.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    wm = await SerdeQueryPathInfoResponse.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    assert wm.valid is False
    json_str = wm.to_json(exclude_unset=True)
    assert '"valid":false' in json_str
    assert '"info"' not in json_str  # info never read from wire → unset field


async def test_wire_version_exclude_unset():
    """Version-skipped fields don't appear in JSON with exclude_unset."""
    # Serialize at 1.38 — first serialize a full result so all fields are written
    # (version-gated fields can't be None at their required wire version)
    br = BuildResult(
        status=0,
        error_msg="ok",
        times_built=1,
        is_non_deterministic=0,
        start_time=100,
        stop_time=200,
        built_outputs={},
    )
    w = BytesWriter()
    await br.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    # Deserialize at version 1.27 — all version-gated fields are skipped
    wm = await BuildResult.from_reader(ReadContext(reader=BytesReader(data), version=proto(1, 27)))
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
    """Roundtrip Realisation — JSON blob with proper Pydantic fields."""
    r = Realisation(
        id=DrvOutput(drvHash="sha256:abc", outputName="out"),  # pyright: ignore[reportCallIssue]
        outPath=SerdeStorePath(path="/nix/store/foo"),  # type: ignore[arg-type]
        signatures=["sig1", "sig2"],
        dependentRealisations={"sha256:xyz!out": "/nix/store/bar"},  # pyright: ignore[reportCallIssue]
    )
    assert r.out_path == SerdeStorePath(path="/nix/store/foo")  # pyright: ignore[reportAttributeAccessIssue]
    assert r.id == DrvOutput(drvHash="sha256:abc", outputName="out")  # pyright: ignore[reportCallIssue, reportAttributeAccessIssue]
    assert r.signatures == ["sig1", "sig2"]

    # Wire roundtrip
    w = BytesWriter()
    await r.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    wm = await Realisation.from_reader(ReadContext(reader=BytesReader(data), version=PROTOCOL_VERSION))
    assert wm.out_path == r.out_path  # pyright: ignore[reportAttributeAccessIssue]
    assert wm.id == r.id  # pyright: ignore[reportAttributeAccessIssue]

    # JSON roundtrip — camelCase keys (Nix wire format)
    json_str = r.to_json()
    parsed = json_lib.loads(json_str)
    assert parsed["outPath"] == "/nix/store/foo"
    assert parsed["id"]["drvHash"] == "sha256:abc"
    assert parsed["id"]["outputName"] == "out"


async def test_wire_signature_roundtrip():
    """Roundtrip Signature — WireString with name/signature properties."""
    sig = Signature(name="cache.nixos.org-1", signature="abc123def456")
    assert sig.name == "cache.nixos.org-1"
    assert sig.signature == "abc123def456"
    assert str(sig) == "cache.nixos.org-1:abc123def456"

    # Wire roundtrip
    w = BytesWriter()
    await sig.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    wm = await Signature.from_reader(ReadContext(reader=BytesReader(data), version=PROTOCOL_VERSION))
    assert wm.name == "cache.nixos.org-1"
    assert wm.signature == "abc123def456"
    assert str(wm) == "cache.nixos.org-1:abc123def456"

    # JSON roundtrip
    json_str = sig.to_json()
    assert json_str == '"cache.nixos.org-1:abc123def456"'


async def test_typed_collections_roundtrip():
    """Roundtrip typed collections — generics handled by _find_reader/_write_value."""
    m = TypedCollections(
        paths={SerdeStorePath(path="/nix/store/a"), SerdeStorePath(path="/nix/store/b")},  # pyright: ignore[reportUnhashable]
        mapping={"key1": SerdeStorePath(path="/nix/store/x")},
    )
    w = BytesWriter()
    await m.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    wm = await TypedCollections.from_reader(ReadContext(reader=BytesReader(w.get_bytes()), version=PROTOCOL_VERSION))
    assert wm.paths == m.paths
    assert wm.mapping == m.mapping


async def test_old_build_result_to_new():
    """Old BuildResult serialize → new BuildResult deserialize → byte-identical re-serialize."""
    old_br = OldBuildResult(
        status=BuildResultStatus.BUILT,
        error_msg="",
        times_built=1,
        is_non_deterministic=0,
        start_time=1000000,
        stop_time=1000500,
        built_outputs={},
        cpu_user=50000,
        cpu_system=10000,
    )
    # old → bytes
    w = BytesWriter()
    await old_br.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()

    # bytes → new
    new_br = await SerdeBuildResult.from_reader(ReadContext(reader=BytesReader(data), version=PROTOCOL_VERSION))
    assert new_br.status == 0
    assert new_br.error_msg == ""
    assert new_br.times_built == 1
    assert new_br.is_non_deterministic == 0
    assert new_br.start_time == 1000000
    assert new_br.stop_time == 1000500
    assert new_br.built_outputs == {}
    assert new_br.cpu_user.tag == 1
    assert new_br.cpu_user.value == 50000
    assert new_br.cpu_system.tag == 1
    assert new_br.cpu_system.value == 10000

    # new → bytes (must be identical)
    w2 = BytesWriter()
    await new_br.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data


async def test_old_basic_derivation_to_new():
    """Old BasicDerivation serialize → new BasicDerivation deserialize → byte-identical re-serialize."""
    old_drv = OldBasicDerivation(
        outputs={"out": OldDerivationOutput(path="/nix/store/xxx", method="", hash_digest="")},
        input_srcs={StorePath("/nix/store/dep1"), StorePath("/nix/store/dep2")},
        platform="x86_64-linux",
        builder="/bin/sh",
        args=["-c", "echo hi"],
        env={"PATH": "/bin", "HOME": "/tmp"},
    )
    # old → bytes
    w = BytesWriter()
    await old_drv.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()

    # bytes → new
    new_drv = await SerdeBasicDerivation.from_reader(ReadContext(reader=BytesReader(data), version=PROTOCOL_VERSION))
    assert new_drv.platform == "x86_64-linux"
    assert new_drv.builder == "/bin/sh"
    assert new_drv.args == ["-c", "echo hi"]
    assert new_drv.env == {"PATH": "/bin", "HOME": "/tmp"}
    assert len(new_drv.outputs) == 1
    assert "out" in new_drv.outputs
    assert new_drv.outputs["out"].path == "/nix/store/xxx"
    assert len(new_drv.input_srcs) == 2
    assert SerdeStorePath(path="/nix/store/dep1") in new_drv.input_srcs
    assert SerdeStorePath(path="/nix/store/dep2") in new_drv.input_srcs

    # new → bytes → new again (content equivalence, sets are unordered)
    w2 = BytesWriter()
    await new_drv.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_drv2 = await SerdeBasicDerivation.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()), version=PROTOCOL_VERSION)
    )
    assert new_drv2.platform == new_drv.platform
    assert new_drv2.builder == new_drv.builder
    assert new_drv2.args == new_drv.args
    assert new_drv2.env == new_drv.env
    assert new_drv2.outputs == new_drv.outputs
    assert new_drv2.input_srcs == new_drv.input_srcs


async def test_old_build_derivation_request_to_new():
    """Old BuildDerivationRequest serialize → new BuildDerivationRequest deserialize."""
    old_drv = OldBasicDerivation(
        outputs={"out": OldDerivationOutput(path="/nix/store/xxx", method="", hash_digest="")},
        input_srcs=set(),
        platform="x86_64-linux",
        builder="/bin/sh",
        args=["-c", "echo hi"],
        env={},
    )
    old_req = OldBuildDerivationRequest(
        drv_path=StorePath("/nix/store/test.drv"),
        derivation=old_drv,
        build_mode=BuildMode.NORMAL,
    )
    # old → bytes
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()

    # bytes → new (skip op uint64 written by old serialize)
    r = BytesReader(data)
    await r.read_uint64()
    new_req = await SerdeBuildDerivationRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    assert str(new_req.drv_path) == "/nix/store/test.drv"
    assert new_req.derivation.platform == "x86_64-linux"
    assert new_req.derivation.builder == "/bin/sh"
    assert new_req.build_mode == 0  # BuildMode.NORMAL as int

    # new → bytes (WireRequest now writes op + body, matching old serialize)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    # Full bytes (op + body) should match old serialize bytes
    assert w2.get_bytes() == data


async def test_old_build_paths_to_new():
    """Old BuildPathsRequest/Response serialize → new serde deserialize."""
    dp1 = DerivedPath("/nix/store/aaa.drv!out")
    dp2 = DerivedPath("/nix/store/bbb.drv!out")

    # Request: old → bytes → new
    old_req = OldBuildPathsRequest(derived_paths={dp1, dp2}, build_mode=BuildMode.NORMAL)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeBuildPathsRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(SerdeDerivedPath(value="/nix/store/aaa.drv!out")) in new_req.derived_paths
    assert str(SerdeDerivedPath(value="/nix/store/bbb.drv!out")) in new_req.derived_paths
    assert new_req.build_mode == 0  # NORMAL

    # Request: new → bytes → content roundtrip (sets are unordered)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_req2 = await SerdeBuildPathsRequest.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()[8:]), version=PROTOCOL_VERSION),
    )
    assert new_req2.derived_paths == new_req.derived_paths
    assert new_req2.build_mode == new_req.build_mode

    # Response: old → bytes → new
    old_resp = OldBuildPathsResponse(value=42)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeBuildPathsResponse.from_reader(read_ctx(data3))
    assert new_resp.value == 42

    # Response: new → bytes (no stderr = just WireLogs empty + value)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_is_valid_path_to_new():
    """Old IsValidPathRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/abc-test")

    # Request: old → bytes → new
    old_req = IsValidPathRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeIsValidPathRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/abc-test"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = IsValidPathResponse(valid=True)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeIsValidPathResponse.from_reader(read_ctx(data3))
    assert new_resp.valid is True

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_query_referrers_to_new():
    """Old QueryReferrersRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/ref-me")

    # Request: old → bytes → new
    old_req = OldQueryReferrersRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryReferrersRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/ref-me"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldQueryReferrersResponse(paths={StorePath("/nix/store/a"), StorePath("/nix/store/b")})
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryReferrersResponse.from_reader(read_ctx(data3))
    assert len(new_resp.paths) == 2
    assert SerdeStorePath(path="/nix/store/a") in new_resp.paths
    assert SerdeStorePath(path="/nix/store/b") in new_resp.paths

    # Response: new → bytes → content roundtrip (sets are unordered)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryReferrersResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.paths == new_resp.paths
