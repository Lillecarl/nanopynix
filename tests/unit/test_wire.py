"""Unit tests for pynixd.wire — wire protocol primitives and operation roundtrips.

Tests are split into two parts:
1. Primitive roundtrips: uint64, string, bytes, set, list, optional, framed.
2. Operation roundtrips: for every operation with from_reader/to_writer,
   construct a realistic instance, serialize via BytesWriter, deserialize
   via BytesReader, and verify field equality.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from pynixd.constants import proto
from pynixd.derived_path import DerivedPath
from pynixd.drv_parser import parse_drv
from pynixd.operations import OP_REGISTRY
from pynixd.operations.add_build_log import AddBuildLogRequest, AddBuildLogResponse
from pynixd.operations.add_indirect_root import AddIndirectRootRequest, AddIndirectRootResponse
from pynixd.operations.add_multiple_to_store import AddMultipleToStoreRequest, AddMultipleToStoreResponse
from pynixd.operations.add_perm_root import AddPermRootRequest, AddPermRootResponse
from pynixd.operations.add_signatures import AddSignaturesRequest, AddSignaturesResponse
from pynixd.operations.add_temp_root import AddTempRootRequest, AddTempRootResponse
from pynixd.operations.add_to_store import AddToStoreRequest, AddToStoreResponse
from pynixd.operations.add_to_store_nar import AddToStoreNarRequest, AddToStoreNarResponse
from pynixd.operations.build_derivation import BuildDerivationRequest, BuildDerivationResponse
from pynixd.operations.build_paths import (
    BuildPathsRequest,
    BuildPathsResponse,
    BuildPathsWithResultsRequest,
    BuildPathsWithResultsResponse,
)
from pynixd.operations.ca_derivations import (
    QueryRealisationRequest,
    QueryRealisationResponse,
    RegisterDrvOutputRequest,
    RegisterDrvOutputResponse,
)
from pynixd.operations.collect_garbage import (
    CollectGarbageRequest,
    CollectGarbageResponse,
    GCAction,
)
from pynixd.operations.ensure_path import EnsurePathRequest, EnsurePathResponse
from pynixd.operations.find_roots import FindRootsEntry, FindRootsRequest, FindRootsResponse
from pynixd.operations.is_valid_path import IsValidPathRequest, IsValidPathResponse
from pynixd.operations.nar_from_path import NarFromPathRequest
from pynixd.operations.optimise_store import OptimiseStoreRequest, OptimiseStoreResponse
from pynixd.operations.query_all_valid_paths import QueryAllValidPathsRequest, QueryAllValidPathsResponse
from pynixd.operations.query_closure import QueryClosureRequest, QueryClosureResponse
from pynixd.operations.query_closure_with_info import QueryClosureWithInfoRequest, QueryClosureWithInfoResponse
from pynixd.operations.query_derivation_output_map import (
    QueryDerivationOutputMapRequest,
    QueryDerivationOutputMapResponse,
)
from pynixd.operations.query_derivation_output_map_batch import (
    DerivationOutputMapBatchResponse,
    QueryDerivationOutputMapBatchRequest,
)
from pynixd.operations.query_missing import QueryMissingRequest, QueryMissingResponse
from pynixd.operations.query_path_from_hash_part import (
    QueryPathFromHashPartRequest,
    QueryPathFromHashPartResponse,
)
from pynixd.operations.query_path_info import QueryPathInfoRequest, QueryPathInfoResponse
from pynixd.operations.query_path_infos import QueryPathInfosRequest, QueryPathInfosResponse
from pynixd.operations.query_referrers import QueryReferrersRequest, QueryReferrersResponse
from pynixd.operations.query_subst_path_info import (
    QuerySubstitutablePathInfoRequest,
    QuerySubstitutablePathInfoResponse,
)
from pynixd.operations.query_subst_path_infos import (
    QuerySubstitutablePathInfosRequest,
    QuerySubstitutablePathInfosResponse,
    SubstitutablePathInfoEntry,
)
from pynixd.operations.query_substitutable_paths import (
    QuerySubstitutablePathsRequest,
    QuerySubstitutablePathsResponse,
)
from pynixd.operations.query_valid_derivers import QueryValidDeriversRequest, QueryValidDeriversResponse
from pynixd.operations.query_valid_paths import QueryValidPathsRequest, QueryValidPathsResponse
from pynixd.operations.set_options import SetOptionsRequest, SetOptionsResponse
from pynixd.operations.sign_path_info import SignPathInfoRequest, SignPathInfoResponse
from pynixd.operations.verify_store import VerifyStoreRequest, VerifyStoreResponse
from pynixd.stderr import StderrNext, StderrStartActivity
from pynixd.store_path import DrvOutput, StorePath
from pynixd.types import KeyedBuildResult
from pynixd.types.ca import Realisation
from pynixd.types.path_info import SubstitutablePathInfo

if TYPE_CHECKING:
    from pynixd.types.aliases import OutputMap
from pynixd.stderr import OperationLogs
from pynixd.types.build import BuildMode, BuildResult, BuildResultStatus
from pynixd.types.context import ReadContext, WriteContext
from pynixd.types.derivation import BasicDerivation, DerivationOutput
from pynixd.types.path_info import UnkeyedValidPathInfo, ValidPathInfo
from pynixd.types.protocol import ActivityType, Verbosity
from pynixd.wire import (
    PROTOCOL_VERSION,
    BytesReader,
    BytesWriter,
    FramedReader,
    FramedWriter,
    NixReader,
    NixWriter,
)

# ═════════════════════════════════════════════════════════════════════════════
# 1. Primitive wire protocol roundtrips
# ═════════════════════════════════════════════════════════════════════════════
from tests.test_features import TestFeatures as F


@pytest.mark.covers(F.WIRE_ENCODE | F.WIRE_DECODE)
class TestWirePrimitives:
    """Roundtrip tests for NixReader/NixWriter primitive methods."""

    async def _roundtrip(self, writer_fn, reader_fn) -> Any:
        w = BytesWriter()
        writer_fn(w)
        r = BytesReader(w.get_bytes())
        return await reader_fn(r)

    async def test_uint64_zero(self):
        val = await self._roundtrip(
            lambda w: w.write_uint64(0),
            lambda r: r.read_uint64(),
        )
        assert val == 0

    async def test_uint64_one(self):
        val = await self._roundtrip(
            lambda w: w.write_uint64(1),
            lambda r: r.read_uint64(),
        )
        assert val == 1

    async def test_uint64_max(self):
        val = await self._roundtrip(
            lambda w: w.write_uint64(2**64 - 1),
            lambda r: r.read_uint64(),
        )
        assert val == 2**64 - 1

    async def test_uint64_large(self):
        val = await self._roundtrip(
            lambda w: w.write_uint64(12345678901234567890),
            lambda r: r.read_uint64(),
        )
        assert val == 12345678901234567890

    async def test_uint64s_empty(self):
        w = BytesWriter()
        w.write_uint64s([])
        assert w.get_bytes() == b""

    async def test_uint64s_single(self):
        w = BytesWriter()
        w.write_uint64s([42])
        r = BytesReader(w.get_bytes())
        assert (await r.read_uint64()) == 42

    async def test_uint64s_multiple(self):
        w = BytesWriter()
        w.write_uint64s([1, 2, 3])
        r = BytesReader(w.get_bytes())
        for expected in [1, 2, 3]:
            assert (await r.read_uint64()) == expected

    async def test_optional_present(self):
        val = await self._roundtrip(
            lambda w: w.write_optional_uint64(42),
            lambda r: r.read_optional_uint64(),
        )
        assert val == 42

    async def test_optional_none(self):
        val = await self._roundtrip(
            lambda w: w.write_optional_uint64(None),
            lambda r: r.read_optional_uint64(),
        )
        assert val is None

    async def test_optional_zero(self):
        val = await self._roundtrip(
            lambda w: w.write_optional_uint64(0),
            lambda r: r.read_optional_uint64(),
        )
        assert val == 0

    async def test_string_empty(self):
        val = await self._roundtrip(
            lambda w: w.write_string(""),
            lambda r: r.read_string(),
        )
        assert val == ""

    async def test_string_ascii(self):
        val = await self._roundtrip(
            lambda w: w.write_string("hello world"),
            lambda r: r.read_string(),
        )
        assert val == "hello world"

    async def test_string_unicode(self):
        val = await self._roundtrip(
            lambda w: w.write_string("héllo wörld 🔥"),
            lambda r: r.read_string(),
        )
        assert val == "héllo wörld 🔥"

    async def test_string_with_storepath(self):
        sp = StorePath("/nix/store/abc123-foo")
        val = await self._roundtrip(
            lambda w: w.write_string(sp),
            lambda r: r.read_string(StorePath),
        )
        assert val == sp
        assert isinstance(val, StorePath)

    async def test_bytes_empty(self):
        val = await self._roundtrip(
            lambda w: w.write_bytes(b""),
            lambda r: r.read_bytes(),
        )
        assert val == b""

    async def test_bytes_small(self):
        val = await self._roundtrip(
            lambda w: w.write_bytes(b"\x00\x01\x02"),
            lambda r: r.read_bytes(),
        )
        assert val == b"\x00\x01\x02"

    async def test_bytes_aligned(self):
        """8 bytes — no padding needed."""
        val = await self._roundtrip(
            lambda w: w.write_bytes(b"\x01" * 8),
            lambda r: r.read_bytes(),
        )
        assert val == b"\x01" * 8

    async def test_bytes_padded(self):
        """5 bytes + 3 padding = 8."""
        val = await self._roundtrip(
            lambda w: w.write_bytes(b"\x01" * 5),
            lambda r: r.read_bytes(),
        )
        assert val == b"\x01" * 5

    async def test_string_list_empty(self):
        val = await self._roundtrip(
            lambda w: w.write_string_list([]),
            lambda r: r.read_string_list(),
        )
        assert val == []

    async def test_string_list_single(self):
        val = await self._roundtrip(
            lambda w: w.write_string_list(["a"]),
            lambda r: r.read_string_list(),
        )
        assert val == ["a"]

    async def test_string_list_multiple(self):
        val = await self._roundtrip(
            lambda w: w.write_string_list(["a", "b", "c"]),
            lambda r: r.read_string_list(),
        )
        assert val == ["a", "b", "c"]

    async def test_string_set_empty(self):
        val = await self._roundtrip(
            lambda w: w.write_string_set(set()),
            lambda r: r.read_string_set(),
        )
        assert val == set()

    async def test_string_set_single(self):
        val = await self._roundtrip(
            lambda w: w.write_string_set({"a"}),
            lambda r: r.read_string_set(),
        )
        assert val == {"a"}

    async def test_string_set_multiple(self):
        val = await self._roundtrip(
            lambda w: w.write_string_set({"a", "b", "c"}),
            lambda r: r.read_string_set(),
        )
        assert val == {"a", "b", "c"}

    async def test_string_set_with_storepath(self):
        paths = {StorePath("/nix/store/a-foo"), StorePath("/nix/store/b-bar")}
        val = await self._roundtrip(
            lambda w: w.write_string_set(paths),
            lambda r: r.read_string_set(StorePath),
        )
        assert val == paths
        assert all(isinstance(p, StorePath) for p in val)


class TestFramedRoundtrip:
    """Roundtrip tests for FramedWriter / FramedReader."""

    async def test_framed_single_chunk(self):
        w = BytesWriter()
        fw = FramedWriter(w)
        fw.write(b"hello")
        await fw.finalize()

        fr = FramedReader(BytesReader(w.get_bytes()))
        data = await fr.readexactly(5)
        assert data == b"hello"

    async def test_framed_multiple_chunks(self):
        w = BytesWriter()
        fw = FramedWriter(w)
        fw.write(b"aaa")
        fw.write(b"bbb")
        await fw.finalize()

        fr = FramedReader(BytesReader(w.get_bytes()))
        data = b""
        while len(data) < 6:
            data += await fr.readexactly(1)
        assert data == b"aaabbb"

    async def test_framed_empty(self):
        w = BytesWriter()
        fw = FramedWriter(w)
        await fw.finalize()

        fr = FramedReader(BytesReader(w.get_bytes()))
        with pytest.raises(EOFError):
            await fr.readexactly(1)

    async def test_framed_ensure_eof(self):
        w = BytesWriter()
        fw = FramedWriter(w)
        fw.write(b"data")
        await fw.finalize()

        fr = FramedReader(BytesReader(w.get_bytes()))
        await fr.readexactly(4)
        await fr.ensure_eof()
        assert fr.at_eof

    async def test_framed_ensure_eof_consumes_extra(self):
        """ensure_eof should consume trailing chunks even if we didn't read them."""
        w = BytesWriter()
        fw = FramedWriter(w)
        fw.write(b"part1")
        fw.write(b"part2")
        await fw.finalize()

        fr = FramedReader(BytesReader(w.get_bytes()))
        await fr.readexactly(5)  # read "part1" only
        await fr.ensure_eof()
        assert fr.at_eof


# ═════════════════════════════════════════════════════════════════════════════
# 2. Operation serialization roundtrips
# ═════════════════════════════════════════════════════════════════════════════

VERSION = PROTOCOL_VERSION  # 1.38 = 294

# ── Helpers for serialization tests ──────────────────────────────────────


async def _serialize_deserialize_request(req_type, req, version=VERSION):
    """Serialize request with serialize, deserialize with deserialize.

    serialize writes [opcode][fields]; deserialize expects [fields] only
    (opcode already consumed by the dispatch loop). So we skip the opcode.
    """
    w = BytesWriter()
    w_ctx = WriteContext(writer=w, version=version)
    await req.serialize(w_ctx)
    r = BytesReader(w.get_bytes())
    op = await r.read_uint64()
    assert op == req_type.op, f"Expected opcode {req_type.op}, got {op}"
    r_ctx = ReadContext(reader=r, version=version)
    return await req_type.deserialize(r_ctx)


async def _serialize_deserialize_response(resp_type, resp, version=VERSION):
    """Serialize response with serialize, deserialize with deserialize, return instance."""
    w = BytesWriter()
    w_ctx = WriteContext(writer=w, version=version)
    await resp.serialize(w_ctx)
    r = BytesReader(w.get_bytes())
    r_ctx = ReadContext(reader=r, version=version, client=None, buffer_logs=False)
    return await resp_type.deserialize(r_ctx)


# ── TestOperationLogs ─────────────────────────────────────────────────────


class TestOperationLogs:
    """OperationLogs serialization roundtrip."""

    async def test_empty_logs(self):
        logs = OperationLogs()
        w = BytesWriter()
        w_ctx = WriteContext(writer=w, version=VERSION)
        logs.serialize(w_ctx)
        r = BytesReader(w.get_bytes())
        r_ctx = ReadContext(reader=r, version=VERSION)
        result = await OperationLogs.deserialize(r_ctx)
        assert result.messages == logs.messages

    async def test_with_next(self):

        logs = OperationLogs()
        logs.add(StderrNext("test log"))
        logs.add(StderrNext("another log"))
        w = BytesWriter()
        w_ctx = WriteContext(writer=w, version=VERSION)
        logs.serialize(w_ctx)
        r = BytesReader(w.get_bytes())
        r_ctx = ReadContext(reader=r, version=VERSION)
        result = await OperationLogs.deserialize(r_ctx)
        assert len(result.messages) == 2
        assert isinstance(result.messages[0], StderrNext)
        assert result.messages[0].text == "test log"
        assert isinstance(result.messages[1], StderrNext)
        assert result.messages[1].text == "another log"

    async def test_with_start_activity(self):

        logs = OperationLogs()
        logs.add(StderrNext("before"))
        logs.add(
            StderrStartActivity(
                act_id=1, level=Verbosity.NOTICE, type=ActivityType.UNKNOWN, text="", fields=["building"], parent=0
            )
        )
        logs.add(StderrNext("after"))
        w = BytesWriter()
        w_ctx = WriteContext(writer=w, version=VERSION)
        logs.serialize(w_ctx)
        r = BytesReader(w.get_bytes())
        r_ctx = ReadContext(reader=r, version=VERSION)
        result = await OperationLogs.deserialize(r_ctx)
        assert len(result.messages) == 3
        assert isinstance(result.messages[1], StderrStartActivity)
        assert result.messages[1].act_id == 1


# ── Helper: create a realistic UnkeyedValidPathInfo for tests ─────────────


def _make_path_info() -> UnkeyedValidPathInfo:
    return UnkeyedValidPathInfo(
        deriver=StorePath("/nix/store/abc-deriver.drv"),
        nar_hash="sha256:abc123def456",
        references={
            StorePath("/nix/store/ref1-bar"),
            StorePath("/nix/store/ref2-baz"),
        },
        registration_time=12345,
        nar_size=999,
        ultimate=1,
        sigs={"key1:sig1"},
        ca="text:sha256:xyz",
    )


def _make_valid_path_info() -> ValidPathInfo:
    return ValidPathInfo(
        path=StorePath("/nix/store/abc123-foo"),
        **_make_path_info().__dict__,
    )


# ── BuildDerivation ────────────────────────────────────────────────────────


class TestBuildDerivationSerialization:
    """BuildDerivation (op 36) uses BasicDerivation + BuildMode in request."""

    async def test_build_derivation_request(self):

        drv = BasicDerivation(
            outputs={"out": DerivationOutput(path="/nix/store/abc-foo")},
            input_srcs=set(),
            platform="x86_64-linux",
            builder="/bin/sh",
            args=["-c", "echo hi"],
            env={"name": "test"},
        )
        req = BuildDerivationRequest(
            drv_path=StorePath("/nix/store/abc-foo.drv"),
            derivation=drv,
            build_mode=BuildMode.NORMAL,
        )
        result = await _serialize_deserialize_request(BuildDerivationRequest, req)
        assert result.drv_path == req.drv_path
        assert result.build_mode == req.build_mode

    async def test_build_derivation_response(self):

        result = BuildResult(status=BuildResultStatus.BUILT, error_msg="")
        resp = BuildDerivationResponse(result=result)
        result2 = await _serialize_deserialize_response(BuildDerivationResponse, resp)
        assert result2.result.status == BuildResultStatus.BUILT
        assert result2.result.error_msg == ""


# ── QueryPathInfo ──────────────────────────────────────────────────────────


class TestQueryPathInfoSerialization:
    """QueryPathInfo (op 26) — request is a StorePath, response is UnkeyedValidPathInfo."""

    async def test_request(self):

        req = QueryPathInfoRequest(path=StorePath("/nix/store/abc-foo"))
        result = await _serialize_deserialize_request(QueryPathInfoRequest, req)
        assert result.path == req.path

    async def test_response(self):

        info = _make_path_info()
        resp = QueryPathInfoResponse(info=info)
        result = await _serialize_deserialize_response(QueryPathInfoResponse, resp)
        # to_writer strips "sha256:" prefix from nar_hash per protocol convention
        assert result.info.nar_hash == info.nar_hash.removeprefix("sha256:")
        assert result.info.nar_size == info.nar_size
        assert result.info.references == info.references


# ── IsValidPath ────────────────────────────────────────────────────────────


class TestIsValidPathSerialization:
    async def test_request(self):

        req = IsValidPathRequest(path=StorePath("/nix/store/abc-foo"))
        result = await _serialize_deserialize_request(IsValidPathRequest, req)
        assert result.path == req.path

    async def test_response(self):

        resp = IsValidPathResponse(valid=True)
        result = await _serialize_deserialize_response(IsValidPathResponse, resp)
        assert result.valid is True

        resp2 = IsValidPathResponse(valid=False)
        result2 = await _serialize_deserialize_response(IsValidPathResponse, resp2)
        assert result2.valid is False


# ── QueryValidPaths ────────────────────────────────────────────────────────


class TestQueryValidPathsSerialization:
    async def test_request(self):

        paths = {StorePath("/nix/store/a-foo"), StorePath("/nix/store/b-bar")}
        req = QueryValidPathsRequest(paths=paths, substitute=1)
        result = await _serialize_deserialize_request(QueryValidPathsRequest, req)
        assert result.paths == paths
        assert result.substitute == 1

    async def test_request_no_substitute(self):
        """substitute field only exists in version >= 1.27."""

        paths = {StorePath("/nix/store/a-foo")}
        req = QueryValidPathsRequest(paths=paths, substitute=0)
        w = BytesWriter()
        w_ctx = WriteContext(writer=w, version=proto(1, 20))
        await req.serialize(w_ctx)
        r = BytesReader(w.get_bytes())
        _ = await r.read_uint64()  # skip opcode
        r_ctx = ReadContext(reader=r, version=proto(1, 20))
        result = await QueryValidPathsRequest.deserialize(r_ctx)
        assert result.paths == paths

    async def test_response(self):

        paths = {StorePath("/nix/store/a-foo"), StorePath("/nix/store/b-bar")}
        resp = QueryValidPathsResponse(paths=paths)
        result = await _serialize_deserialize_response(QueryValidPathsResponse, resp)
        assert result.paths == paths


# ── AddSignatures ──────────────────────────────────────────────────────────


class TestAddSignaturesSerialization:
    async def test_request(self):

        req = AddSignaturesRequest(
            path=StorePath("/nix/store/abc-foo"),
            sigs={"key1:sig1", "key2:sig2"},
        )
        result = await _serialize_deserialize_request(AddSignaturesRequest, req)
        assert result.path == req.path
        assert result.sigs == req.sigs

    async def test_response(self):

        resp = AddSignaturesResponse(value=0)
        result = await _serialize_deserialize_response(AddSignaturesResponse, resp)
        assert result.value == 0

        resp = AddSignaturesResponse(value=1)
        result = await _serialize_deserialize_response(AddSignaturesResponse, resp)
        assert result.value == 1


# ── QueryReferrers ─────────────────────────────────────────────────────────


class TestQueryReferrersSerialization:
    async def test_request(self):

        req = QueryReferrersRequest(path=StorePath("/nix/store/abc-foo"))
        result = await _serialize_deserialize_request(QueryReferrersRequest, req)
        assert result.path == req.path

    async def test_response(self):

        paths = {StorePath("/nix/store/a-foo"), StorePath("/nix/store/b-bar")}
        resp = QueryReferrersResponse(paths=paths)
        result = await _serialize_deserialize_response(QueryReferrersResponse, resp)
        assert result.paths == paths


# ── EnsurePath ─────────────────────────────────────────────────────────────


class TestEnsurePathSerialization:
    async def test_request(self):

        req = EnsurePathRequest(path=StorePath("/nix/store/abc-foo"))
        result = await _serialize_deserialize_request(EnsurePathRequest, req)
        assert result.path == req.path

    async def test_response(self):

        resp = EnsurePathResponse(value=1)
        result = await _serialize_deserialize_response(EnsurePathResponse, resp)
        assert result.value == 1


# ── AddTempRoot ────────────────────────────────────────────────────────────


class TestAddTempRootSerialization:
    async def test_request(self):

        req = AddTempRootRequest(path=StorePath("/nix/store/abc-foo"))
        result = await _serialize_deserialize_request(AddTempRootRequest, req)
        assert result.path == req.path

    async def test_response(self):

        resp = AddTempRootResponse(value=1)
        result = await _serialize_deserialize_response(AddTempRootResponse, resp)
        assert result.value == 1


# ── AddIndirectRoot ────────────────────────────────────────────────────────


class TestAddIndirectRootSerialization:
    async def test_request(self):

        req = AddIndirectRootRequest(path=StorePath("/nix/store/abc-foo"))
        result = await _serialize_deserialize_request(AddIndirectRootRequest, req)
        assert result.path == req.path

    async def test_response(self):

        resp = AddIndirectRootResponse(value=1)
        result = await _serialize_deserialize_response(AddIndirectRootResponse, resp)
        assert result.value == 1


# ── AddPermRoot ────────────────────────────────────────────────────────────


class TestAddPermRootSerialization:
    async def test_request(self):

        req = AddPermRootRequest(store_path="/nix/store/abc-foo", gc_root="/nix/var/nix/gcroots/foo")
        result = await _serialize_deserialize_request(AddPermRootRequest, req)
        assert result.store_path == req.store_path
        assert result.gc_root == req.gc_root

    async def test_response(self):

        resp = AddPermRootResponse(gc_root="/nix/var/nix/gcroots/foo")
        result = await _serialize_deserialize_response(AddPermRootResponse, resp)
        assert result.gc_root == resp.gc_root


# ── FindRoots ──────────────────────────────────────────────────────────────


class TestFindRootsSerialization:
    async def test_request(self):

        req = FindRootsRequest()
        result = await _serialize_deserialize_request(FindRootsRequest, req)
        assert result == req  # no fields

    async def test_response(self):

        resp = FindRootsResponse(
            roots=[FindRootsEntry(link="/proc/1", target="/nix/store/abc-foo")],
        )
        result = await _serialize_deserialize_response(FindRootsResponse, resp)
        assert len(result.roots) == 1
        assert result.roots[0].link == "/proc/1"
        assert result.roots[0].target == "/nix/store/abc-foo"


# ── QueryPathFromHashPart ──────────────────────────────────────────────────


class TestQueryPathFromHashPartSerialization:
    async def test_request(self):

        req = QueryPathFromHashPartRequest(path="abc123")
        result = await _serialize_deserialize_request(QueryPathFromHashPartRequest, req)
        assert result.path == req.path

    async def test_response(self):

        resp = QueryPathFromHashPartResponse(value=StorePath("/nix/store/abc123-foo"))
        result = await _serialize_deserialize_response(QueryPathFromHashPartResponse, resp)
        assert result.value == resp.value


# ── QuerySubstitutablePaths ────────────────────────────────────────────────


class TestQuerySubstitutablePathsSerialization:
    async def test_request(self):

        paths = {StorePath("/nix/store/a-foo"), StorePath("/nix/store/b-bar")}
        req = QuerySubstitutablePathsRequest(paths=paths)
        result = await _serialize_deserialize_request(QuerySubstitutablePathsRequest, req)
        assert result.paths == paths

    async def test_response(self):

        paths = {StorePath("/nix/store/a-foo")}
        resp = QuerySubstitutablePathsResponse(paths=paths)
        result = await _serialize_deserialize_response(QuerySubstitutablePathsResponse, resp)
        assert result.paths == paths


# ── QueryValidDerivers ─────────────────────────────────────────────────────


class TestQueryValidDeriversSerialization:
    async def test_request(self):

        req = QueryValidDeriversRequest(path=StorePath("/nix/store/abc-foo"))
        result = await _serialize_deserialize_request(QueryValidDeriversRequest, req)
        assert result.path == req.path

    async def test_response(self):

        paths = {StorePath("/nix/store/abc-bar.drv")}
        resp = QueryValidDeriversResponse(paths=paths)
        result = await _serialize_deserialize_response(QueryValidDeriversResponse, resp)
        assert result.paths == paths


# ── SetOptions ─────────────────────────────────────────────────────────────


class TestSetOptionsSerialization:
    async def test_request(self):

        req = SetOptionsRequest(
            keep_failed=0,
            keep_going=1,
            try_fallback=0,
            verbosity=Verbosity.ERROR,
            max_build_jobs=4,
            max_silent_time=0,
            _obsolete_use_build_hook=0,
            build_verbosity=Verbosity.ERROR,
            _obsolete_log_type=0,
            _obsolete_print_build_trace=0,
            build_cores=2,
            use_substitutes=1,
            overrides={"foo": "bar"},
        )
        result = await _serialize_deserialize_request(SetOptionsRequest, req)
        assert result.keep_failed == 0
        assert result.keep_going == 1
        assert result.max_build_jobs == 4
        assert result.build_cores == 2
        assert result.overrides == {"foo": "bar"}

    async def test_response(self):

        resp = SetOptionsResponse()
        await _serialize_deserialize_response(SetOptionsResponse, resp)
        # Response has no payload, just logs

    async def test_request_no_overrides(self):
        """Overrides dict only exists in version >= 1.12."""

        req = SetOptionsRequest(
            keep_failed=0,
            keep_going=0,
            try_fallback=0,
            verbosity=Verbosity.ERROR,
            max_build_jobs=0,
            max_silent_time=0,
            _obsolete_use_build_hook=0,
            build_verbosity=Verbosity.ERROR,
            _obsolete_log_type=0,
            _obsolete_print_build_trace=0,
            build_cores=0,
            use_substitutes=0,
            overrides={},
        )
        w = BytesWriter()
        w_ctx = WriteContext(writer=w, version=proto(1, 10))
        await req.serialize(w_ctx)
        r = BytesReader(w.get_bytes())
        # Skip opcode
        _ = await r.read_uint64()
        r_ctx = ReadContext(reader=r, version=proto(1, 10))
        result = await SetOptionsRequest.deserialize(r_ctx)
        assert result.overrides == {}


# ── CollectGarbage ─────────────────────────────────────────────────────────


class TestCollectGarbageSerialization:
    async def test_request(self):

        req = CollectGarbageRequest(
            action=GCAction.RETURN_DEAD,
            paths_to_delete={StorePath("/nix/store/abc-foo")},
            ignore_liveness=0,
            max_freed=1000000,
            _obsolete1=0,
            _obsolete2=0,
            _obsolete3=0,
        )
        result = await _serialize_deserialize_request(CollectGarbageRequest, req)
        assert result.action == GCAction.RETURN_DEAD
        assert result.paths_to_delete == {StorePath("/nix/store/abc-foo")}
        assert result.max_freed == 1000000

    async def test_response(self):

        resp = CollectGarbageResponse(
            paths_deleted={StorePath("/nix/store/abc-foo")},
            bytes_freed=42,
            _obsolete=0,
        )
        result = await _serialize_deserialize_response(CollectGarbageResponse, resp)
        assert result.paths_deleted == {StorePath("/nix/store/abc-foo")}
        assert result.bytes_freed == 42


# ── QueryAllValidPaths ─────────────────────────────────────────────────────


class TestQueryAllValidPathsSerialization:
    async def test_request(self):

        req = QueryAllValidPathsRequest()
        await _serialize_deserialize_request(QueryAllValidPathsRequest, req)
        # No fields

    async def test_response(self):

        paths = {StorePath("/nix/store/a-foo"), StorePath("/nix/store/b-bar")}
        resp = QueryAllValidPathsResponse(paths=paths)
        result = await _serialize_deserialize_response(QueryAllValidPathsResponse, resp)
        assert result.paths == paths


# ── QueryDerivationOutputMap ───────────────────────────────────────────────


class TestQueryDerivationOutputMapSerialization:
    async def test_request(self):

        req = QueryDerivationOutputMapRequest(path=StorePath("/nix/store/abc.drv"))
        result = await _serialize_deserialize_request(QueryDerivationOutputMapRequest, req)
        assert result.path == req.path

    async def test_response(self):

        resp = QueryDerivationOutputMapResponse(
            items={"out": StorePath("/nix/store/abc-foo"), "lib": None},
        )
        result = await _serialize_deserialize_response(QueryDerivationOutputMapResponse, resp)
        assert result.items["out"] == StorePath("/nix/store/abc-foo")
        assert result.items["lib"] is None


# ── QueryMissing ───────────────────────────────────────────────────────────


class TestQueryMissingSerialization:
    async def test_request(self):

        req = QueryMissingRequest(
            derived_paths={
                DerivedPath("/nix/store/abc.drv!out"),
            },
        )
        result = await _serialize_deserialize_request(QueryMissingRequest, req)
        assert result.derived_paths == req.derived_paths

    async def test_response(self):

        resp = QueryMissingResponse(
            will_build=set(),
            will_substitute=set(),
            unknown=set(),
            download_size=0,
            nar_size=0,
        )
        result = await _serialize_deserialize_response(QueryMissingResponse, resp)
        assert result.will_build == set()
        assert result.download_size == 0


# ── OptimiseStore ──────────────────────────────────────────────────────────


class TestOptimiseStoreSerialization:
    async def test_request(self):

        req = OptimiseStoreRequest()
        await _serialize_deserialize_request(OptimiseStoreRequest, req)
        # No fields

    async def test_response(self):

        resp = OptimiseStoreResponse(value=0)
        result = await _serialize_deserialize_response(OptimiseStoreResponse, resp)
        assert result.value == 0


# ── VerifyStore ────────────────────────────────────────────────────────────


class TestVerifyStoreSerialization:
    async def test_request(self):

        req = VerifyStoreRequest(check_contents=1, repair=0)
        result = await _serialize_deserialize_request(VerifyStoreRequest, req)
        assert result.check_contents == 1
        assert result.repair == 0

    async def test_response(self):

        resp = VerifyStoreResponse(value=0)
        result = await _serialize_deserialize_response(VerifyStoreResponse, resp)
        assert result.value == 0


# ── AddBuildLog ────────────────────────────────────────────────────────────


class TestAddBuildLogSerialization:
    async def test_request(self):

        req = AddBuildLogRequest(path=StorePath("/nix/store/abc-foo"))
        result = await _serialize_deserialize_request(AddBuildLogRequest, req)
        assert result.path == req.path

    async def test_response(self):

        resp = AddBuildLogResponse(value=0)
        result = await _serialize_deserialize_response(AddBuildLogResponse, resp)
        assert result.value == 0


# ── BuildPaths / BuildPathsWithResults ─────────────────────────────────────


class TestBuildPathsSerialization:
    async def test_request(self):

        req = BuildPathsRequest(
            derived_paths={DerivedPath("/nix/store/abc.drv!out")},
            build_mode=BuildMode.NORMAL,
        )
        result = await _serialize_deserialize_request(BuildPathsRequest, req)
        assert result.derived_paths == req.derived_paths
        assert result.build_mode == req.build_mode

    async def test_response(self):

        resp = BuildPathsResponse(value=0)
        result = await _serialize_deserialize_response(BuildPathsResponse, resp)
        assert result.value == 0


class TestBuildPathsWithResultsSerialization:
    async def test_request(self):

        req = BuildPathsWithResultsRequest(
            derived_paths={DerivedPath("/nix/store/abc.drv!out")},
            build_mode=BuildMode.NORMAL,
        )
        result = await _serialize_deserialize_request(BuildPathsWithResultsRequest, req)
        assert result.derived_paths == req.derived_paths
        assert result.build_mode == req.build_mode

    async def test_response(self):

        result = BuildResult(status=BuildResultStatus.BUILT, error_msg="")
        resp = BuildPathsWithResultsResponse(
            results=[KeyedBuildResult(path=DerivedPath("/nix/store/abc.drv!out"), result=result)],
        )
        result2 = await _serialize_deserialize_response(BuildPathsWithResultsResponse, resp)
        assert len(result2.results) == 1
        assert result2.results[0].result.status == BuildResultStatus.BUILT


# ── SubstitutablePathInfo variants ────────────────────────────────────────


class TestQuerySubstitutablePathInfoSerialization:
    async def test_request(self):

        req = QuerySubstitutablePathInfoRequest(path="/nix/store/abc-foo")
        result = await _serialize_deserialize_request(QuerySubstitutablePathInfoRequest, req)
        assert result.path == req.path

    async def test_response_found(self):

        info = SubstitutablePathInfo(
            deriver=StorePath("/nix/store/d.drv"),
            references={StorePath("/nix/store/r1")},
            download_size=100,
            nar_size=200,
        )
        resp = QuerySubstitutablePathInfoResponse(found=True, info=info)
        result = await _serialize_deserialize_response(
            QuerySubstitutablePathInfoResponse,
            resp,
        )
        assert result.found is True
        assert result.info is not None
        assert result.info.download_size == 100

    async def test_response_not_found(self):

        resp = QuerySubstitutablePathInfoResponse(found=False, info=None)
        result = await _serialize_deserialize_response(
            QuerySubstitutablePathInfoResponse,
            resp,
        )
        assert result.found is False
        assert result.info is None


class TestQuerySubstitutablePathInfosSerialization:
    async def test_request(self):

        req = QuerySubstitutablePathInfosRequest(items={"/nix/store/abc": ""})
        result = await _serialize_deserialize_request(QuerySubstitutablePathInfosRequest, req)
        assert result.items == req.items

    async def test_response(self):

        info = SubstitutablePathInfo(
            deriver=StorePath("/nix/store/d.drv"),
            references=set(),
            download_size=50,
            nar_size=75,
        )
        resp = QuerySubstitutablePathInfosResponse(
            entries=[SubstitutablePathInfoEntry(path="/nix/store/abc", info=info)],
        )
        result = await _serialize_deserialize_response(
            QuerySubstitutablePathInfosResponse,
            resp,
        )
        # buffer_logs=False means no messages get logged
        assert len(result.logs.messages) == 0
        assert len(result.entries) == 1
        assert result.entries[0].path == "/nix/store/abc"
        assert result.entries[0].info.download_size == 50


# ── QueryRealisation (CA) ─────────────────────────────────────────────────


class TestQueryRealisationSerialization:
    async def test_request(self):

        req = QueryRealisationRequest(
            drv_output=DrvOutput("sha256:abc123!out"),
        )
        result = await _serialize_deserialize_request(QueryRealisationRequest, req)
        assert result.drv_output == req.drv_output

    async def test_response(self):

        from pynixd.store_path import DrvOutput

        drv_out = DrvOutput(hash_algo="sha256", hash_value="abc", output_name="test")
        resp = QueryRealisationResponse(
            realisations=[Realisation(id=drv_out, outPath=StorePath("/nix/store/test"))],
        )
        result = await _serialize_deserialize_response(QueryRealisationResponse, resp)
        assert result.realisations == resp.realisations


class TestRegisterDrvOutputSerialization:
    async def test_request(self):

        from pynixd.store_path import DrvOutput

        drv_out = DrvOutput(hash_algo="sha256", hash_value="abc", output_name="test")
        req = RegisterDrvOutputRequest(
            realisation=Realisation(id=drv_out, outPath=StorePath("/nix/store/test")),
        )
        result = await _serialize_deserialize_request(RegisterDrvOutputRequest, req)
        assert result.realisation == req.realisation

    async def test_response(self):

        resp = RegisterDrvOutputResponse()
        await _serialize_deserialize_response(RegisterDrvOutputResponse, resp)


# ── NarFromPath / AddToStoreNar (streaming headers only) ──────────────────


class TestNarFromPathSerialization:
    """NarFromPath request has wire serialization even though response is streaming."""

    async def test_request(self):

        req = NarFromPathRequest(path=StorePath("/nix/store/abc-foo"), nar_size=0)
        result = await _serialize_deserialize_request(NarFromPathRequest, req)
        assert result.path == req.path


class TestAddToStoreNarSerialization:
    async def test_request(self):

        info = _make_valid_path_info()
        req = AddToStoreNarRequest(info=info, repair=0, dont_check_sigs=0)
        result = await _serialize_deserialize_request(AddToStoreNarRequest, req)
        assert result.info.path == info.path
        assert result.repair == 0

    async def test_response(self):

        resp = AddToStoreNarResponse()
        await _serialize_deserialize_response(AddToStoreNarResponse, resp)


class TestAddMultipleToStoreSerialization:
    async def test_request(self):

        req = AddMultipleToStoreRequest(repair=0, dont_check_sigs=0)
        result = await _serialize_deserialize_request(AddMultipleToStoreRequest, req)
        assert result.repair == 0

    async def test_response(self):

        resp = AddMultipleToStoreResponse()
        await _serialize_deserialize_response(AddMultipleToStoreResponse, resp)


# ── Extension operations (not wire-dispatched, but have serialization) ────


class TestSignPathInfoSerialization:
    async def test_request(self):

        info = _make_valid_path_info()
        req = SignPathInfoRequest(info=info)
        result = await _serialize_deserialize_request(SignPathInfoRequest, req)
        assert result.info.path == info.path

    async def test_response(self):

        info = _make_valid_path_info()
        resp = SignPathInfoResponse(info=info)
        result = await _serialize_deserialize_response(SignPathInfoResponse, resp)
        assert result.info.path == info.path


# ── AddToStore (streaming, but request has wire format) ────────────────────


class TestAddToStoreSerialization:
    async def test_request(self):

        req = AddToStoreRequest(
            path_name="test",
            cam="text:sha256",
            references={StorePath("/nix/store/ref1"), StorePath("/nix/store/ref2")},
            repair=0,
        )
        result = await _serialize_deserialize_request(AddToStoreRequest, req)
        assert result.path_name == req.path_name

    async def test_response(self):

        info = _make_valid_path_info()
        resp = AddToStoreResponse(info=info)
        result = await _serialize_deserialize_response(AddToStoreResponse, resp)
        assert result.info.path == info.path
        # to_writer strips "sha256:" prefix from nar_hash per protocol convention
        assert result.info.nar_hash == info.nar_hash.removeprefix("sha256:")


# ── Extension ops (103-106) ────────────────────────────────────────────────


class TestQueryPathInfosSerialization:
    """QueryPathInfos (op 103, extension) — batch path info query."""

    async def test_request(self):

        paths = {StorePath("/nix/store/a-foo"), StorePath("/nix/store/b-bar")}
        req = QueryPathInfosRequest(paths=paths)
        result = await _serialize_deserialize_request(QueryPathInfosRequest, req)
        assert result.paths == paths

    async def test_response(self):

        info = _make_valid_path_info()
        resp = QueryPathInfosResponse(infos={info.path: info})
        result = await _serialize_deserialize_response(QueryPathInfosResponse, resp)
        assert info.path in result.infos
        assert result.infos[info.path].nar_hash == info.nar_hash.removeprefix("sha256:")


class TestQueryClosureSerialization:
    """QueryClosure (op 104, extension) — closure query."""

    async def test_request(self):

        paths = {StorePath("/nix/store/a-foo")}
        req = QueryClosureRequest(paths=paths)
        result = await _serialize_deserialize_request(QueryClosureRequest, req)
        assert result.paths == paths

    async def test_response(self):

        paths = {StorePath("/nix/store/a-foo"), StorePath("/nix/store/b-bar")}
        resp = QueryClosureResponse(paths=paths)
        result = await _serialize_deserialize_response(QueryClosureResponse, resp)
        assert result.paths == paths


class TestQueryClosureWithInfoSerialization:
    """QueryClosureWithInfo (op 105, extension) — closure with path info."""

    async def test_request(self):

        paths = {StorePath("/nix/store/a-foo")}
        req = QueryClosureWithInfoRequest(paths=paths)
        result = await _serialize_deserialize_request(QueryClosureWithInfoRequest, req)
        assert result.paths == paths

    async def test_response(self):

        info = _make_valid_path_info()
        resp = QueryClosureWithInfoResponse(infos=[info])
        result = await _serialize_deserialize_response(QueryClosureWithInfoResponse, resp)
        assert len(result.infos) == 1
        assert result.infos[0].path == info.path


class TestDerivationOutputMapBatchSerialization:
    """QueryDerivationOutputMapBatch (op 106, extension) — batch output map query."""

    async def test_request(self):

        paths = {StorePath("/nix/store/a.drv"), StorePath("/nix/store/b.drv")}
        req = QueryDerivationOutputMapBatchRequest(drv_paths=paths)
        result = await _serialize_deserialize_request(QueryDerivationOutputMapBatchRequest, req)
        assert result.drv_paths == paths

    async def test_response(self):

        outputs: OutputMap = {
            StorePath("/nix/store/a.drv"): {"out": StorePath("/nix/store/a-foo")},
            StorePath("/nix/store/b.drv"): {
                "out": StorePath("/nix/store/b-bar"),
                "lib": StorePath("/nix/store/b-lib"),
            },
        }
        resp = DerivationOutputMapBatchResponse(outputs=outputs)
        result = await _serialize_deserialize_response(DerivationOutputMapBatchResponse, resp)
        assert len(result.outputs) == 2
        assert result.outputs[StorePath("/nix/store/a.drv")] == {"out": StorePath("/nix/store/a-foo")}
        assert result.outputs[StorePath("/nix/store/b.drv")]["lib"] == StorePath("/nix/store/b-lib")
