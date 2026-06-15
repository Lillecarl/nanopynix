"""Binary wire roundtrip and cross-serialization tests."""

from __future__ import annotations

import json as json_lib

import pytest

from pynixd.constants import PROTOCOL_VERSION, proto
from pynixd.derived_path import DerivedPath
from pynixd.operations.add_build_log import (
    AddBuildLogRequest as OldAddBuildLogRequest,
)
from pynixd.operations.add_build_log import (
    AddBuildLogResponse as OldAddBuildLogResponse,
)
from pynixd.operations.add_indirect_root import (
    AddIndirectRootRequest as OldAddIndirectRootRequest,
)
from pynixd.operations.add_indirect_root import (
    AddIndirectRootResponse as OldAddIndirectRootResponse,
)
from pynixd.operations.add_multiple_to_store import (
    AddMultipleToStoreRequest as OldAddMultipleToStoreRequest,
)
from pynixd.operations.add_multiple_to_store import (
    AddMultipleToStoreResponse as OldAddMultipleToStoreResponse,
)
from pynixd.operations.add_perm_root import (
    AddPermRootRequest as OldAddPermRootRequest,
)
from pynixd.operations.add_perm_root import (
    AddPermRootResponse as OldAddPermRootResponse,
)
from pynixd.operations.add_signatures import (
    AddSignaturesRequest as OldAddSignaturesRequest,
)
from pynixd.operations.add_signatures import (
    AddSignaturesResponse as OldAddSignaturesResponse,
)
from pynixd.operations.add_temp_root import (
    AddTempRootRequest as OldAddTempRootRequest,
)
from pynixd.operations.add_temp_root import (
    AddTempRootResponse as OldAddTempRootResponse,
)
from pynixd.operations.add_to_store_nar import (
    AddToStoreNarRequest as OldAddToStoreNarRequest,
)
from pynixd.operations.add_to_store_nar import (
    AddToStoreNarResponse as OldAddToStoreNarResponse,
)
from pynixd.operations.build_derivation import (
    BuildDerivationRequest as OldBuildDerivationRequest,
)
from pynixd.operations.build_paths import (
    BuildPathsRequest as OldBuildPathsRequest,
)
from pynixd.operations.build_paths import (
    BuildPathsResponse as OldBuildPathsResponse,
)
from pynixd.operations.build_paths import (
    BuildPathsWithResultsRequest as OldBuildPathsWithResultsRequest,
)
from pynixd.operations.build_paths import (
    BuildPathsWithResultsResponse as OldBuildPathsWithResultsResponse,
)
from pynixd.operations.ca_derivations import (
    QueryRealisationRequest as OldQueryRealisationRequest,
)
from pynixd.operations.ca_derivations import (
    QueryRealisationResponse as OldQueryRealisationResponse,
)
from pynixd.operations.ca_derivations import (
    RegisterDrvOutputRequest as OldRegisterDrvOutputRequest,
)
from pynixd.operations.ca_derivations import (
    RegisterDrvOutputResponse as OldRegisterDrvOutputResponse,
)
from pynixd.operations.collect_garbage import (
    CollectGarbageRequest as OldCollectGarbageRequest,
)
from pynixd.operations.collect_garbage import (
    CollectGarbageResponse as OldCollectGarbageResponse,
)
from pynixd.operations.ensure_path import (
    EnsurePathRequest as OldEnsurePathRequest,
)
from pynixd.operations.ensure_path import (
    EnsurePathResponse as OldEnsurePathResponse,
)
from pynixd.operations.find_roots import (
    FindRootsEntry as OldFindRootsEntry,
)
from pynixd.operations.find_roots import (
    FindRootsRequest as OldFindRootsRequest,
)
from pynixd.operations.find_roots import (
    FindRootsResponse as OldFindRootsResponse,
)
from pynixd.operations.is_valid_path import (
    IsValidPathRequest,
    IsValidPathResponse,
)
from pynixd.operations.nar_from_path import (
    NarFromPathRequest as OldNarFromPathRequest,
)
from pynixd.operations.optimise_store import (
    OptimiseStoreRequest as OldOptimiseStoreRequest,
)
from pynixd.operations.optimise_store import (
    OptimiseStoreResponse as OldOptimiseStoreResponse,
)
from pynixd.operations.pynixd_collect_garbage import (
    PynixdCollectGarbageRequest as OldPynixdCollectGarbageRequest,
)
from pynixd.operations.pynixd_collect_garbage import (
    PynixdCollectGarbageResponse as OldPynixdCollectGarbageResponse,
)
from pynixd.operations.query_all_valid_paths import (
    QueryAllValidPathsRequest as OldQueryAllValidPathsRequest,
)
from pynixd.operations.query_all_valid_paths import (
    QueryAllValidPathsResponse as OldQueryAllValidPathsResponse,
)
from pynixd.operations.query_closure import (
    QueryClosureRequest as OldQueryClosureRequest,
)
from pynixd.operations.query_closure import (
    QueryClosureResponse as OldQueryClosureResponse,
)
from pynixd.operations.query_derivation_output_map import (
    QueryDerivationOutputMapRequest as OldQueryDerivationOutputMapRequest,
)
from pynixd.operations.query_derivation_output_map import (
    QueryDerivationOutputMapResponse as OldQueryDerivationOutputMapResponse,
)
from pynixd.operations.query_missing import (
    QueryMissingRequest as OldQueryMissingRequest,
)
from pynixd.operations.query_missing import (
    QueryMissingResponse as OldQueryMissingResponse,
)
from pynixd.operations.query_path_from_hash_part import (
    QueryPathFromHashPartRequest as OldQueryPathFromHashPartRequest,
)
from pynixd.operations.query_path_from_hash_part import (
    QueryPathFromHashPartResponse as OldQueryPathFromHashPartResponse,
)
from pynixd.operations.query_path_info import QueryPathInfoRequest as OldQueryPathInfoRequest
from pynixd.operations.query_path_info import QueryPathInfoResponse
from pynixd.operations.query_path_infos import (
    QueryPathInfosRequest as OldQueryPathInfosRequest,
)
from pynixd.operations.query_path_infos import (
    QueryPathInfosResponse as OldQueryPathInfosResponse,
)
from pynixd.operations.query_referrers import (
    QueryReferrersRequest as OldQueryReferrersRequest,
)
from pynixd.operations.query_referrers import (
    QueryReferrersResponse as OldQueryReferrersResponse,
)
from pynixd.operations.query_substitutable_paths import (
    QuerySubstitutablePathsRequest as OldQuerySubstitutablePathsRequest,
)
from pynixd.operations.query_substitutable_paths import (
    QuerySubstitutablePathsResponse as OldQuerySubstitutablePathsResponse,
)
from pynixd.operations.query_valid_derivers import (
    QueryValidDeriversRequest as OldQueryValidDeriversRequest,
)
from pynixd.operations.query_valid_derivers import (
    QueryValidDeriversResponse as OldQueryValidDeriversResponse,
)
from pynixd.operations.query_valid_paths import (
    QueryValidPathsRequest as OldQueryValidPathsRequest,
)
from pynixd.operations.query_valid_paths import (
    QueryValidPathsResponse as OldQueryValidPathsResponse,
)
from pynixd.operations.set_options import (
    SetOptionsRequest as OldSetOptionsRequest,
)
from pynixd.operations.set_options import (
    SetOptionsResponse as OldSetOptionsResponse,
)
from pynixd.operations.verify_store import (
    VerifyStoreRequest as OldVerifyStoreRequest,
)
from pynixd.operations.verify_store import (
    VerifyStoreResponse as OldVerifyStoreResponse,
)
from pynixd.serde import (
    AddBuildLogRequest as SerdeAddBuildLogRequest,
)
from pynixd.serde import (
    AddBuildLogResponse as SerdeAddBuildLogResponse,
)
from pynixd.serde import (
    AddIndirectRootRequest as SerdeAddIndirectRootRequest,
)
from pynixd.serde import (
    AddIndirectRootResponse as SerdeAddIndirectRootResponse,
)
from pynixd.serde import (
    AddMultipleToStoreRequest as SerdeAddMultipleToStoreRequest,
)
from pynixd.serde import (
    AddMultipleToStoreResponse as SerdeAddMultipleToStoreResponse,
)
from pynixd.serde import (
    AddPermRootRequest as SerdeAddPermRootRequest,
)
from pynixd.serde import (
    AddPermRootResponse as SerdeAddPermRootResponse,
)
from pynixd.serde import (
    AddSignaturesRequest as SerdeAddSignaturesRequest,
)
from pynixd.serde import (
    AddSignaturesResponse as SerdeAddSignaturesResponse,
)
from pynixd.serde import (
    AddTempRootRequest as SerdeAddTempRootRequest,
)
from pynixd.serde import (
    AddTempRootResponse as SerdeAddTempRootResponse,
)
from pynixd.serde import (
    AddToStoreNarRequest as SerdeAddToStoreNarRequest,
)
from pynixd.serde import (
    AddToStoreNarResponse as SerdeAddToStoreNarResponse,
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
    BuildPathsWithResultsRequest as SerdeBuildPathsWithResultsRequest,
)
from pynixd.serde import (
    BuildPathsWithResultsResponse as SerdeBuildPathsWithResultsResponse,
)
from pynixd.serde import (
    BuildResult as SerdeBuildResult,
)
from pynixd.serde import (
    CollectGarbageRequest as SerdeCollectGarbageRequest,
)
from pynixd.serde import (
    CollectGarbageResponse as SerdeCollectGarbageResponse,
)
from pynixd.serde import (
    DerivationOutput as SerdeDerivationOutput,
)
from pynixd.serde import (
    DerivedPath as SerdeDerivedPath,
)
from pynixd.serde import (
    EnsurePathRequest as SerdeEnsurePathRequest,
)
from pynixd.serde import (
    EnsurePathResponse as SerdeEnsurePathResponse,
)
from pynixd.serde import (
    FindRootsEntry as SerdeFindRootsEntry,
)
from pynixd.serde import (
    FindRootsRequest as SerdeFindRootsRequest,
)
from pynixd.serde import (
    FindRootsResponse as SerdeFindRootsResponse,
)
from pynixd.serde import (
    IsValidPathRequest as SerdeIsValidPathRequest,
)
from pynixd.serde import (
    IsValidPathResponse as SerdeIsValidPathResponse,
)
from pynixd.serde import (
    KeyedBuildResult as SerdeKeyedBuildResult,
)
from pynixd.serde import (
    NarFromPathRequest as SerdeNarFromPathRequest,
)
from pynixd.serde import (
    NarFromPathResponse as SerdeNarFromPathResponse,
)
from pynixd.serde import (
    OptimiseStoreRequest as SerdeOptimiseStoreRequest,
)
from pynixd.serde import (
    OptimiseStoreResponse as SerdeOptimiseStoreResponse,
)
from pynixd.serde import (
    PynixdCollectGarbageRequest as SerdePynixdCollectGarbageRequest,
)
from pynixd.serde import (
    PynixdCollectGarbageResponse as SerdePynixdCollectGarbageResponse,
)
from pynixd.serde import (
    PynixdGCAction as SerdePynixdGCAction,
)
from pynixd.serde import (
    QueryAllValidPathsRequest as SerdeQueryAllValidPathsRequest,
)
from pynixd.serde import (
    QueryAllValidPathsResponse as SerdeQueryAllValidPathsResponse,
)
from pynixd.serde import (
    QueryClosureRequest as SerdeQueryClosureRequest,
)
from pynixd.serde import (
    QueryClosureResponse as SerdeQueryClosureResponse,
)
from pynixd.serde import (
    QueryDerivationOutputMapRequest as SerdeQueryDerivationOutputMapRequest,
)
from pynixd.serde import (
    QueryDerivationOutputMapResponse as SerdeQueryDerivationOutputMapResponse,
)
from pynixd.serde import (
    QueryMissingRequest as SerdeQueryMissingRequest,
)
from pynixd.serde import (
    QueryMissingResponse as SerdeQueryMissingResponse,
)
from pynixd.serde import (
    QueryPathFromHashPartRequest as SerdeQueryPathFromHashPartRequest,
)
from pynixd.serde import (
    QueryPathFromHashPartResponse as SerdeQueryPathFromHashPartResponse,
)
from pynixd.serde import (
    QueryPathInfoRequest as SerdeQueryPathInfoRequest,
)
from pynixd.serde import (
    QueryPathInfoResponse as SerdeQueryPathInfoResponse,
)
from pynixd.serde import (
    QueryPathInfosRequest as SerdeQueryPathInfosRequest,
)
from pynixd.serde import (
    QueryPathInfosResponse as SerdeQueryPathInfosResponse,
)
from pynixd.serde import (
    QueryRealisationRequest as SerdeQueryRealisationRequest,
)
from pynixd.serde import (
    QueryRealisationResponse as SerdeQueryRealisationResponse,
)
from pynixd.serde import (
    QueryReferrersRequest as SerdeQueryReferrersRequest,
)
from pynixd.serde import (
    QueryReferrersResponse as SerdeQueryReferrersResponse,
)
from pynixd.serde import (
    QuerySubstitutablePathsRequest as SerdeQuerySubstitutablePathsRequest,
)
from pynixd.serde import (
    QuerySubstitutablePathsResponse as SerdeQuerySubstitutablePathsResponse,
)
from pynixd.serde import (
    QueryValidDeriversRequest as SerdeQueryValidDeriversRequest,
)
from pynixd.serde import (
    QueryValidDeriversResponse as SerdeQueryValidDeriversResponse,
)
from pynixd.serde import (
    QueryValidPathsRequest as SerdeQueryValidPathsRequest,
)
from pynixd.serde import (
    QueryValidPathsResponse as SerdeQueryValidPathsResponse,
)
from pynixd.serde import (
    RegisterDrvOutputRequest as SerdeRegisterDrvOutputRequest,
)
from pynixd.serde import (
    RegisterDrvOutputResponse as SerdeRegisterDrvOutputResponse,
)
from pynixd.serde import (
    SetOptionsRequest as SerdeSetOptionsRequest,
)
from pynixd.serde import (
    SetOptionsResponse as SerdeSetOptionsResponse,
)
from pynixd.serde import (
    StorePath as SerdeStorePath,
)
from pynixd.serde import (
    UnkeyedValidPathInfo as SerdeUnkeyedValidPathInfo,
)
from pynixd.serde import (
    VerifyStoreRequest as SerdeVerifyStoreRequest,
)
from pynixd.serde import (
    VerifyStoreResponse as SerdeVerifyStoreResponse,
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
from pynixd.types import GCAction as GCAction
from pynixd.types import PynixdGCAction as OldPynixdGCAction
from pynixd.types.build import (
    KeyedBuildResult as OldKeyedBuildResult,
)
from pynixd.types.context import ReadContext, WriteContext
from pynixd.types.path_info import UnkeyedValidPathInfo
from pynixd.types.protocol import Verbosity
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
        id=DrvOutput(drv_hash="sha256:abc", output_name="out"),
        outPath=SerdeStorePath(path="/nix/store/foo"),  # type: ignore[arg-type]
        signatures=["sig1", "sig2"],
        dependentRealisations={"sha256:xyz!out": "/nix/store/bar"},  # pyright: ignore[reportCallIssue]
    )
    assert r.out_path == SerdeStorePath(path="/nix/store/foo")  # pyright: ignore[reportAttributeAccessIssue]
    assert r.id == DrvOutput(drv_hash="sha256:abc", output_name="out")
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
    assert parsed["id"] == "sha256:abc!out"


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


async def test_old_build_paths_with_results_to_new():
    """Old BuildPathsWithResults serialize → new serde deserialize."""
    from pynixd.operations.build_paths import (
        BuildPathsWithResultsRequest as OldBPWRReq,
    )
    from pynixd.operations.build_paths import (
        BuildPathsWithResultsResponse as OldBPWRResp,
    )
    from pynixd.serde import (
        BuildPathsWithResultsRequest as NewBPWRReq,
    )
    from pynixd.serde import (
        BuildPathsWithResultsResponse as NewBPWRResp,
    )
    from pynixd.types.build import BuildResult as OldBuildResult
    from pynixd.types.build import KeyedBuildResult as OldKBR

    dp1 = DerivedPath("/nix/store/aaa.drv!out")

    # Request: old → bytes → new
    old_req = OldBPWRReq(derived_paths={dp1}, build_mode=BuildMode.NORMAL)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await NewBPWRReq.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert new_req.build_mode == 0
    assert len(new_req.derived_paths) == 1

    # Request: new → bytes → body matches old body
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_br = OldBuildResult(status=BuildResultStatus.BUILT, error_msg="")
    old_kbr = OldKBR(path=dp1, result=old_br)
    old_resp = OldBPWRResp(results=[old_kbr])
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await NewBPWRResp.from_reader(read_ctx(data3))
    assert len(new_resp.results) == 1
    assert str(new_resp.results[0].path) == "/nix/store/aaa.drv!out"
    assert new_resp.results[0].result.status == 0


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


async def test_old_ensure_path_to_new():
    """Old EnsurePathRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/abc-test")

    # Request: old → bytes → new
    old_req = OldEnsurePathRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeEnsurePathRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/abc-test"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldEnsurePathResponse(value=42)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeEnsurePathResponse.from_reader(read_ctx(data3))
    assert new_resp.value == 42

    # Response: new → bytes (no stderr = just WireLogs empty + value)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_add_temp_root_to_new():
    """Old AddTempRootRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/tmp-root")

    # Request: old → bytes → new
    old_req = OldAddTempRootRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeAddTempRootRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/tmp-root"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldAddTempRootResponse(value=1)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeAddTempRootResponse.from_reader(read_ctx(data3))
    assert new_resp.value == 1

    # Response: new → bytes (no stderr = just WireLogs empty + value)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_add_indirect_root_to_new():
    """Old AddIndirectRootRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/indirect-root")

    # Request: old → bytes → new
    old_req = OldAddIndirectRootRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeAddIndirectRootRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/indirect-root"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldAddIndirectRootResponse(value=1)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeAddIndirectRootResponse.from_reader(read_ctx(data3))
    assert new_resp.value == 1

    # Response: new → bytes (no stderr = just WireLogs empty + value)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_find_roots_to_new():
    """Old FindRootsRequest/Response serialize → new serde deserialize."""

    # Request: old → bytes → new (no body, just op)
    old_req = OldFindRootsRequest()
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeFindRootsRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert isinstance(new_req, SerdeFindRootsRequest)

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldFindRootsResponse(
        roots=[
            OldFindRootsEntry(link="/proc/1/map_files/a", target="/nix/store/xxx"),
            OldFindRootsEntry(link="/proc/1/map_files/b", target="/nix/store/yyy"),
        ]
    )
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeFindRootsResponse.from_reader(read_ctx(data3))
    assert len(new_resp.roots) == 2
    assert new_resp.roots[0].link == "/proc/1/map_files/a"
    assert new_resp.roots[0].target == "/nix/store/xxx"
    assert new_resp.roots[1].link == "/proc/1/map_files/b"
    assert new_resp.roots[1].target == "/nix/store/yyy"

    # Response: new → bytes (no stderr = just WireLogs empty + roots)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_set_options_to_new():
    """Old SetOptionsRequest/Response serialize → new serde deserialize."""

    # Request: old → bytes → new
    old_req = OldSetOptionsRequest(
        keep_failed=0,
        keep_going=1,
        try_fallback=0,
        verbosity=Verbosity(0),
        max_build_jobs=8,
        max_silent_time=3600,
        _obsolete_use_build_hook=0,
        build_verbosity=Verbosity(2),
        _obsolete_log_type=0,
        _obsolete_print_build_trace=0,
        build_cores=4,
        use_substitutes=1,
        overrides={},
    )
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeSetOptionsRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert new_req.keep_failed == 0
    assert new_req.keep_going == 1
    assert new_req.try_fallback == 0
    assert new_req.verbosity == 0
    assert new_req.max_build_jobs == 8
    assert new_req.max_silent_time == 3600
    assert new_req.obsolete_use_build_hook == 0
    assert new_req.build_verbosity == 2
    assert new_req.obsolete_log_type == 0
    assert new_req.obsolete_print_build_trace == 0
    assert new_req.build_cores == 4
    assert new_req.use_substitutes == 1
    assert new_req.overrides == {}

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldSetOptionsResponse()
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeSetOptionsResponse.from_reader(read_ctx(data3))
    assert isinstance(new_resp, SerdeSetOptionsResponse)

    # Response: new → bytes (no stderr = just WireLogs empty)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_collect_garbage_to_new():
    """Old CollectGarbageRequest/Response serialize → new serde deserialize."""
    sp1 = StorePath("/nix/store/delete-me-1")
    sp2 = StorePath("/nix/store/delete-me-2")

    # Request: old → bytes → new
    old_req = OldCollectGarbageRequest(
        action=GCAction.DELETE_DEAD,
        paths_to_delete={sp1, sp2},
        ignore_liveness=0,
        max_freed=1000000,
        _obsolete1=0,
        _obsolete2=0,
        _obsolete3=0,
    )
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeCollectGarbageRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert new_req.action == GCAction.DELETE_DEAD
    assert len(new_req.paths_to_delete) == 2
    assert SerdeStorePath(path="/nix/store/delete-me-1") in new_req.paths_to_delete
    assert SerdeStorePath(path="/nix/store/delete-me-2") in new_req.paths_to_delete
    assert new_req.ignore_liveness == 0
    assert new_req.max_freed == 1000000
    assert new_req.obsolete1 == 0
    assert new_req.obsolete2 == 0
    assert new_req.obsolete3 == 0

    # Request: new → bytes → content roundtrip (sets are unordered)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_req2 = await SerdeCollectGarbageRequest.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()[8:]), version=PROTOCOL_VERSION),
    )
    assert new_req2.action == new_req.action
    assert new_req2.paths_to_delete == new_req.paths_to_delete
    assert new_req2.ignore_liveness == new_req.ignore_liveness
    assert new_req2.max_freed == new_req.max_freed
    assert new_req2.obsolete1 == new_req.obsolete1
    assert new_req2.obsolete2 == new_req.obsolete2
    assert new_req2.obsolete3 == new_req.obsolete3

    # Response: old → bytes → new
    old_resp = OldCollectGarbageResponse(
        paths_deleted={sp1},
        bytes_freed=500000,
        _obsolete=0,
    )
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeCollectGarbageResponse.from_reader(read_ctx(data3))
    assert len(new_resp.paths_deleted) == 1
    assert SerdeStorePath(path="/nix/store/delete-me-1") in new_resp.paths_deleted
    assert new_resp.bytes_freed == 500000
    assert new_resp.obsolete == 0

    # Response: new → bytes → content roundtrip (single-element set, but be safe)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeCollectGarbageResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.paths_deleted == new_resp.paths_deleted
    assert new_resp2.bytes_freed == new_resp.bytes_freed
    assert new_resp2.obsolete == new_resp.obsolete


async def test_old_query_all_valid_paths_to_new():
    """Old QueryAllValidPathsRequest/Response serialize → new serde deserialize."""
    sp1 = StorePath("/nix/store/a-valid")
    sp2 = StorePath("/nix/store/b-valid")

    # Request: old → bytes → new (no body, just op)
    old_req = OldQueryAllValidPathsRequest()
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryAllValidPathsRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert isinstance(new_req, SerdeQueryAllValidPathsRequest)

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldQueryAllValidPathsResponse(paths={sp1, sp2})
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryAllValidPathsResponse.from_reader(read_ctx(data3))
    assert len(new_resp.paths) == 2
    assert SerdeStorePath(path="/nix/store/a-valid") in new_resp.paths
    assert SerdeStorePath(path="/nix/store/b-valid") in new_resp.paths

    # Response: new → bytes → content roundtrip (sets are unordered)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryAllValidPathsResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.paths == new_resp.paths


async def test_old_query_path_info_to_new():
    """Old QueryPathInfoRequest serialize → new serde deserialize."""
    sp = StorePath("/nix/store/query-me")

    # Request: old → bytes → new
    old_req = OldQueryPathInfoRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryPathInfoRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/query-me"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response roundtrip already tested in test_query_path_info_response_roundtrip


async def test_old_query_path_infos_to_new():
    """Old QueryPathInfosRequest/Response serialize → new serde deserialize."""
    sp1 = StorePath("/nix/store/info-1")
    sp2 = StorePath("/nix/store/info-2")

    # Request: old → bytes → new
    old_req = OldQueryPathInfosRequest(paths={sp1, sp2})
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryPathInfosRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert len(new_req.paths) == 2
    assert SerdeStorePath(path="/nix/store/info-1") in new_req.paths
    assert SerdeStorePath(path="/nix/store/info-2") in new_req.paths

    # Request: new → bytes → content roundtrip (sets are unordered)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_req2 = await SerdeQueryPathInfosRequest.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()[8:]), version=PROTOCOL_VERSION),
    )
    assert new_req2.paths == new_req.paths

    # Response: old → bytes → new
    from pynixd.types.path_info import (
        UnkeyedValidPathInfo as OldUnkeyedValidPathInfo,
    )
    from pynixd.types.path_info import (
        ValidPathInfo as OldValidPathInfo,
    )

    uinfo1 = OldUnkeyedValidPathInfo(
        nar_hash="sha256:abc",
        references=set(),
        registration_time=100,
        nar_size=1024,
        ultimate=0,
        sigs=set(),
        ca="",
    )
    info1 = uinfo1.with_path(sp1)

    uinfo2 = OldUnkeyedValidPathInfo(
        nar_hash="sha256:def",
        references={StorePath("/nix/store/ref-a")},
        registration_time=200,
        nar_size=2048,
        ultimate=1,
        sigs={"key1:abc123"},
        ca="fixed:r:sha256:xyz",
    )
    info2 = uinfo2.with_path(sp2)

    old_resp = OldQueryPathInfosResponse(infos={sp1: info1, sp2: info2})
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryPathInfosResponse.from_reader(read_ctx(data3))
    assert len(new_resp.infos) == 2
    # Find by store path (order may vary)
    info_map = {str(i.path): i for i in new_resp.infos}
    assert "/nix/store/info-1" in info_map
    assert "/nix/store/info-2" in info_map
    i1 = info_map["/nix/store/info-1"]
    assert str(i1.info.nar_hash) == "abc"  # sha256: stripped on wire
    assert i1.info.nar_size == 1024
    assert i1.info.ultimate is False
    i2 = info_map["/nix/store/info-2"]
    assert str(i2.info.nar_hash) == "def"
    assert i2.info.nar_size == 2048
    assert i2.info.ultimate is True
    assert SerdeStorePath(path="/nix/store/ref-a") in i2.info.references

    # Response: new → bytes → content roundtrip
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryPathInfosResponse.from_reader(read_ctx(w4.get_bytes()))
    assert len(new_resp2.infos) == 2
    info_map2 = {str(i.path): i for i in new_resp2.infos}
    assert "/nix/store/info-1" in info_map2
    assert "/nix/store/info-2" in info_map2


async def test_old_query_closure_to_new():
    """Old QueryClosureRequest/Response serialize → new serde deserialize."""
    sp1 = StorePath("/nix/store/closure-a")
    sp2 = StorePath("/nix/store/closure-b")
    sp3 = StorePath("/nix/store/closure-c")

    # Request: old → bytes → new
    old_req = OldQueryClosureRequest(paths={sp1, sp2})
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryClosureRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert len(new_req.paths) == 2
    assert SerdeStorePath(path="/nix/store/closure-a") in new_req.paths
    assert SerdeStorePath(path="/nix/store/closure-b") in new_req.paths

    # Request: new → bytes → content roundtrip (sets are unordered)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_req2 = await SerdeQueryClosureRequest.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()[8:]), version=PROTOCOL_VERSION),
    )
    assert new_req2.paths == new_req.paths

    # Response: old → bytes → new
    old_resp = OldQueryClosureResponse(paths={sp1, sp2, sp3})
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryClosureResponse.from_reader(read_ctx(data3))
    assert len(new_resp.paths) == 3
    assert SerdeStorePath(path="/nix/store/closure-a") in new_resp.paths
    assert SerdeStorePath(path="/nix/store/closure-b") in new_resp.paths
    assert SerdeStorePath(path="/nix/store/closure-c") in new_resp.paths

    # Response: new → bytes → content roundtrip
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryClosureResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.paths == new_resp.paths


async def test_old_query_path_from_hash_part_to_new():
    """Old QueryPathFromHashPartRequest/Response serialize → new serde deserialize."""

    # Request: old → bytes → new
    old_req = OldQueryPathFromHashPartRequest(path="abc123")
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryPathFromHashPartRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert new_req.path == "abc123"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldQueryPathFromHashPartResponse(value=StorePath("/nix/store/abc123-foo"))
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryPathFromHashPartResponse.from_reader(read_ctx(data3))
    assert str(new_resp.value) == "/nix/store/abc123-foo"

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_query_valid_paths_to_new():
    """Old QueryValidPathsRequest/Response serialize → new serde deserialize."""
    sp1 = StorePath("/nix/store/valid-1")
    sp2 = StorePath("/nix/store/valid-2")

    # Request: old → bytes → new
    old_req = OldQueryValidPathsRequest(paths={sp1, sp2}, substitute=0)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryValidPathsRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert len(new_req.paths) == 2
    assert SerdeStorePath(path="/nix/store/valid-1") in new_req.paths
    assert SerdeStorePath(path="/nix/store/valid-2") in new_req.paths
    assert new_req.substitute == 0

    # Request: new → bytes → content roundtrip (sets are unordered)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_req2 = await SerdeQueryValidPathsRequest.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()[8:]), version=PROTOCOL_VERSION),
    )
    assert new_req2.paths == new_req.paths
    assert new_req2.substitute == new_req.substitute

    # Response: old → bytes → new
    old_resp = OldQueryValidPathsResponse(paths={sp1})
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryValidPathsResponse.from_reader(read_ctx(data3))
    assert len(new_resp.paths) == 1
    assert SerdeStorePath(path="/nix/store/valid-1") in new_resp.paths

    # Response: new → bytes → content roundtrip (single-element set)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryValidPathsResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.paths == new_resp.paths


async def test_old_query_substitutable_paths_to_new():
    """Old QuerySubstitutablePathsRequest/Response serialize → new serde deserialize."""
    sp1 = StorePath("/nix/store/sub-1")
    sp2 = StorePath("/nix/store/sub-2")

    # Request: old → bytes → new
    old_req = OldQuerySubstitutablePathsRequest(paths={sp1, sp2})
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQuerySubstitutablePathsRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert len(new_req.paths) == 2
    assert SerdeStorePath(path="/nix/store/sub-1") in new_req.paths
    assert SerdeStorePath(path="/nix/store/sub-2") in new_req.paths

    # Request: new → bytes → content roundtrip (sets are unordered)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_req2 = await SerdeQuerySubstitutablePathsRequest.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()[8:]), version=PROTOCOL_VERSION),
    )
    assert new_req2.paths == new_req.paths

    # Response: old → bytes → new
    old_resp = OldQuerySubstitutablePathsResponse(paths={sp1})
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQuerySubstitutablePathsResponse.from_reader(read_ctx(data3))
    assert len(new_resp.paths) == 1
    assert SerdeStorePath(path="/nix/store/sub-1") in new_resp.paths

    # Response: new → bytes → content roundtrip (single-element set)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQuerySubstitutablePathsResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.paths == new_resp.paths


async def test_old_query_valid_derivers_to_new():
    """Old QueryValidDeriversRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/some-drv.drv")

    # Request: old → bytes → new
    old_req = OldQueryValidDeriversRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryValidDeriversRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/some-drv.drv"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    drv1 = StorePath("/nix/store/deriver-1")
    drv2 = StorePath("/nix/store/deriver-2")
    old_resp = OldQueryValidDeriversResponse(paths={drv1, drv2})
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryValidDeriversResponse.from_reader(read_ctx(data3))
    assert len(new_resp.paths) == 2
    assert SerdeStorePath(path="/nix/store/deriver-1") in new_resp.paths
    assert SerdeStorePath(path="/nix/store/deriver-2") in new_resp.paths

    # Response: new → bytes → content roundtrip (sets are unordered)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryValidDeriversResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.paths == new_resp.paths


async def test_old_optimise_store_to_new():
    """Old OptimiseStoreRequest/Response serialize → new serde deserialize."""

    # Request: old → bytes → new (no body, just op)
    old_req = OldOptimiseStoreRequest()
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeOptimiseStoreRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert isinstance(new_req, SerdeOptimiseStoreRequest)

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldOptimiseStoreResponse(value=42)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeOptimiseStoreResponse.from_reader(read_ctx(data3))
    assert new_resp.value == 42

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_verify_store_to_new():
    """Old VerifyStoreRequest/Response serialize → new serde deserialize."""

    # Request: old → bytes → new
    old_req = OldVerifyStoreRequest(check_contents=1, repair=0)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeVerifyStoreRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert new_req.check_contents == 1
    assert new_req.repair == 0

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldVerifyStoreResponse(value=7)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeVerifyStoreResponse.from_reader(read_ctx(data3))
    assert new_resp.value == 7

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_nar_from_path_to_new():
    """Old NarFromPathRequest serialize → new serde deserialize."""
    sp = StorePath("/nix/store/nar-me")

    # Request: old → bytes → new
    old_req = OldNarFromPathRequest(path=sp, nar_size=0)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeNarFromPathRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/nar-me"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data


async def test_old_add_signatures_to_new():
    """Old AddSignaturesRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/sig-me")

    # Request: old → bytes → new
    old_req = OldAddSignaturesRequest(path=sp, sigs={"sig-abc", "sig-def"})
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeAddSignaturesRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/sig-me"
    assert len(new_req.sigs) == 2
    sig_names = {s.name for s in new_req.sigs}
    assert "sig-abc" in sig_names
    assert "sig-def" in sig_names

    # Request: new → bytes → content roundtrip (sets are unordered)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_req2 = await SerdeAddSignaturesRequest.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()[8:]), version=PROTOCOL_VERSION),
    )
    assert new_req2.path == new_req.path
    assert new_req2.sigs == new_req.sigs

    # Response: old → bytes → new
    old_resp = OldAddSignaturesResponse(value=1)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeAddSignaturesResponse.from_reader(read_ctx(data3))
    assert new_resp.value == 1

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_add_to_store_nar_to_new():
    """Old AddToStoreNarRequest serialize → new serde deserialize (header only)."""
    from pynixd.types.path_info import ValidPathInfo as OldValidPathInfo

    # Build old-style ValidPathInfo (flat dataclass, not nested)
    old_info = OldValidPathInfo(
        path=StorePath("/nix/store/nar-item"),
        nar_hash="sha256:abc123",
        nar_size=1024,
        registration_time=1000,
    )

    # Request: old → bytes → new
    old_req = OldAddToStoreNarRequest(
        info=old_info,
        repair=0,
        dont_check_sigs=1,
    )
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeAddToStoreNarRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.info.path) == "/nix/store/nar-item"
    assert new_req.repair == 0
    assert new_req.dont_check_sigs == 1

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new (empty body)
    old_resp = OldAddToStoreNarResponse()
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeAddToStoreNarResponse.from_reader(read_ctx(data3))
    assert isinstance(new_resp, SerdeAddToStoreNarResponse)

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_add_multiple_to_store_to_new():
    """Old AddMultipleToStoreRequest/Response serialize → new serde deserialize (header only)."""

    # Request: old → bytes → new
    old_req = OldAddMultipleToStoreRequest(repair=1, dont_check_sigs=0)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeAddMultipleToStoreRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert new_req.repair == 1
    assert new_req.dont_check_sigs == 0

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new (empty body)
    old_resp = OldAddMultipleToStoreResponse()
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeAddMultipleToStoreResponse.from_reader(read_ctx(data3))
    assert isinstance(new_resp, SerdeAddMultipleToStoreResponse)

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_add_build_log_to_new():
    """Old AddBuildLogRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/log-me")

    # Request: old → bytes → new
    old_req = OldAddBuildLogRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeAddBuildLogRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/log-me"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldAddBuildLogResponse(value=42)
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeAddBuildLogResponse.from_reader(read_ctx(data3))
    assert new_resp.value == 42

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_query_missing_to_new():
    """Old QueryMissingRequest/Response serialize → new serde deserialize."""
    dp1 = DerivedPath("/nix/store/drv1.drv!out")
    dp2 = DerivedPath("/nix/store/drv2.drv!dev")

    # Request: old → bytes → new
    old_req = OldQueryMissingRequest(derived_paths={dp1, dp2})
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryMissingRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert len(new_req.derived_paths) == 2

    # Request: new → bytes → content roundtrip (sets are unordered)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    new_req2 = await SerdeQueryMissingRequest.from_reader(
        ReadContext(reader=BytesReader(w2.get_bytes()[8:]), version=PROTOCOL_VERSION),
    )
    assert new_req2.derived_paths == new_req.derived_paths

    # Response: old → bytes → new
    sb1 = StorePath("/nix/store/will-sub-1")
    wb1 = StorePath("/nix/store/will-build-1")
    old_resp = OldQueryMissingResponse(
        will_build={wb1},
        will_substitute={sb1},
        unknown=set(),
        download_size=1024,
        nar_size=2048,
    )
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryMissingResponse.from_reader(read_ctx(data3))
    assert SerdeStorePath(path="/nix/store/will-build-1") in new_resp.will_build
    assert SerdeStorePath(path="/nix/store/will-sub-1") in new_resp.will_substitute
    assert len(new_resp.unknown) == 0
    assert new_resp.download_size == 1024
    assert new_resp.nar_size == 2048

    # Response: new → bytes → content roundtrip
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryMissingResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.will_build == new_resp.will_build
    assert new_resp2.will_substitute == new_resp.will_substitute
    assert new_resp2.unknown == new_resp.unknown
    assert new_resp2.download_size == new_resp.download_size
    assert new_resp2.nar_size == new_resp.nar_size


async def test_old_query_derivation_output_map_to_new():
    """Old QueryDerivationOutputMapRequest/Response serialize → new serde deserialize."""
    sp = StorePath("/nix/store/some-drv.drv")

    # Request: old → bytes → new
    old_req = OldQueryDerivationOutputMapRequest(path=sp)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryDerivationOutputMapRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.path) == "/nix/store/some-drv.drv"

    # Request: new → bytes → full bytes match
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldQueryDerivationOutputMapResponse(
        items={"out": StorePath("/nix/store/out-path"), "dev": None},
    )
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryDerivationOutputMapResponse.from_reader(read_ctx(data3))
    assert len(new_resp.items) == 2
    assert str(new_resp.items["out"]) == "/nix/store/out-path"
    # None in old → empty StorePath in new (wire has empty string)
    assert str(new_resp.items["dev"]) == ""

    # Response: new → bytes → content roundtrip
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryDerivationOutputMapResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.items == new_resp.items


async def test_old_register_drv_output_to_new():
    """Old RegisterDrvOutput serialize → new serde deserialize."""
    from pynixd.operations.ca_derivations import RegisterDrvOutputRequest as OldRDOReq
    from pynixd.serde import Realisation as NewRealisation
    from pynixd.serde import RegisterDrvOutputRequest as NewRDOReq
    from pynixd.store_path import DrvOutput as OldDrvOutput
    from pynixd.types.ca import Realisation as OldRealisation

    old_real = OldRealisation(
        id=OldDrvOutput("sha256:abc!out"),
        outPath=StorePath("/nix/store/foo"),
        signatures=["sig1"],
        dependentRealisations={},
    )
    old_req = OldRDOReq(realisation=old_real)

    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()

    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await NewRDOReq.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    assert new_req.realisation.id == "sha256:abc!out"
    # Old wire sends StorePath.base() (no /nix/store/ prefix)
    assert str(new_req.realisation.out_path) == "foo"
    assert new_req.realisation.signatures == ["sig1"]

    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes()[8:] == data[8:]


async def test_old_query_realisation_to_new():
    """Old QueryRealisationRequest/Response serialize → new serde deserialize."""
    from pynixd.store_path import DrvOutput as OldDrvOutput
    from pynixd.types.ca import Realisation as OldRealisation

    # Request: old → bytes → new
    old_req = OldQueryRealisationRequest(
        drv_output=OldDrvOutput("sha256:abc!out"),
    )
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeQueryRealisationRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert str(new_req.drv_output) == "sha256:abc!out"

    # Request: new → bytes → full bytes match (skip op)
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes()[8:] == data[8:]

    # Response: old → bytes → new
    from pynixd.store_path import DrvOutput as OldDrvOutput

    old_real = OldRealisation(
        id=OldDrvOutput("sha256:abc!out"),
        outPath=StorePath("/nix/store/foo"),
        signatures=["sig1"],
    )
    old_resp = OldQueryRealisationResponse(realisations=[old_real])
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeQueryRealisationResponse.from_reader(read_ctx(data3))
    assert len(new_resp.realisations) == 1
    assert new_resp.realisations[0].id == "sha256:abc!out"
    assert new_resp.realisations[0].signatures == ["sig1"]

    # Response: new → bytes → content roundtrip
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdeQueryRealisationResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.realisations[0].id == new_resp.realisations[0].id
    assert new_resp2.realisations[0].signatures == new_resp.realisations[0].signatures


async def test_old_add_perm_root_to_new():
    """Old AddPermRootRequest/Response serialize → new serde deserialize."""

    # Request: old → bytes → new
    old_req = OldAddPermRootRequest(store_path="/nix/store/perm-1", gc_root="/nix/var/nix/gcroots/perm-1")
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdeAddPermRootRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert new_req.store_path == "/nix/store/perm-1"
    assert new_req.gc_root == "/nix/var/nix/gcroots/perm-1"

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Response: old → bytes → new
    old_resp = OldAddPermRootResponse(gc_root="/nix/var/nix/gcroots/perm-1")
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdeAddPermRootResponse.from_reader(read_ctx(data3))
    assert new_resp.gc_root == "/nix/var/nix/gcroots/perm-1"

    # Response: new → bytes
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    assert w4.get_bytes() == data3


async def test_old_pynixd_collect_garbage_to_new():
    """Old PynixdCollectGarbageRequest/Response serialize → new serde deserialize."""

    # Request: old → bytes → new
    old_req = OldPynixdCollectGarbageRequest(action=OldPynixdGCAction.DRY_RUN)
    w = BytesWriter()
    await old_req.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
    data = w.get_bytes()
    r = BytesReader(data)
    await r.read_uint64()  # skip op
    new_req = await SerdePynixdCollectGarbageRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    assert new_req.action == SerdePynixdGCAction.DRY_RUN

    # Request: new → bytes → full bytes match old serialize
    w2 = BytesWriter()
    await new_req.to_writer(WriteContext(writer=w2, version=PROTOCOL_VERSION))
    assert w2.get_bytes() == data

    # Request: EXECUTE variant
    old_req2 = OldPynixdCollectGarbageRequest(action=OldPynixdGCAction.EXECUTE)
    w5 = BytesWriter()
    await old_req2.serialize(WriteContext(writer=w5, version=PROTOCOL_VERSION))
    data5 = w5.get_bytes()
    r5 = BytesReader(data5)
    await r5.read_uint64()
    new_req2 = await SerdePynixdCollectGarbageRequest.from_reader(ReadContext(reader=r5, version=PROTOCOL_VERSION))
    assert new_req2.action == SerdePynixdGCAction.EXECUTE

    w6 = BytesWriter()
    await new_req2.to_writer(WriteContext(writer=w6, version=PROTOCOL_VERSION))
    assert w6.get_bytes() == data5

    # Response: old → bytes → new
    sp1 = StorePath("/nix/store/gc-d-1")
    sp2 = StorePath("/nix/store/gc-d-2")
    old_resp = OldPynixdCollectGarbageResponse(
        store_paths={sp1, sp2},
        bytes=123456,
    )
    w3 = BytesWriter()
    await old_resp.serialize(WriteContext(writer=w3, version=PROTOCOL_VERSION))
    data3 = w3.get_bytes()
    new_resp = await SerdePynixdCollectGarbageResponse.from_reader(read_ctx(data3))
    assert len(new_resp.store_paths) == 2
    assert SerdeStorePath(path="/nix/store/gc-d-1") in new_resp.store_paths
    assert SerdeStorePath(path="/nix/store/gc-d-2") in new_resp.store_paths
    assert new_resp.bytes == 123456

    # Response: new → bytes → content roundtrip (sets are unordered)
    w4 = BytesWriter()
    await new_resp.to_writer(WriteContext(writer=w4, version=PROTOCOL_VERSION))
    new_resp2 = await SerdePynixdCollectGarbageResponse.from_reader(read_ctx(w4.get_bytes()))
    assert new_resp2.store_paths == new_resp.store_paths
    assert new_resp2.bytes == new_resp.bytes


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
