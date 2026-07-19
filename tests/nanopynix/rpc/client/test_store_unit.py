"""Unit tests for Store facade — mock WorkerPool, validate coercion logic.

No Nix daemon needed.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false, reportOptionalMemberAccess=false, reportOptionalSubscript=false
# These are suppressed file-wide because every test uses MagicMock objects whose
# members are inherently unknown at the type-checker level.

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nanopynix_proto.nix.store import (
    AddIndirectRootRequest,
    AddPermRootRequest,
    AddTempRootRequest,
    AddToStoreRequest,
    AddToStoreResponse,
    BuildPathsWithResultsRequest,
    CollectGarbageRequest,
    ComputeFsClosureRequest,
    ComputeStorePathRequest,
    ComputeStorePathResponse,
    EnsurePathRequest,
    FindRootsRequest,
    FollowLinksToStorePathRequest,
    FollowLinksToStorePathResponse,
    GcAction,
    GetBuildLogRequest,
    GetStoreDirRequest,
    GetUriRequest,
    IsValidPathRequest,
    OptimiseStoreRequest,
    ParseStorePathRequest,
    ParseStorePathResponse,
    QueryAllValidPathsRequest,
    QueryDerivationOutputsRequest,
    QueryMissingRequest,
    QueryPathFromHashPartRequest,
    QueryPathInfoRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
    ReadDerivationRequest,
    VerifyStoreRequest,
)

import nanopynix
from nanopynix.rpc.client.store import Store as PublicStore
from nanopynix.rpc.client.store import StoreHandle as Store


def _make_stub_mock() -> MagicMock:
    """Create a MagicMock that acts as a StoreServiceStub.

    All methods return AsyncMocks by default.
    """
    stub = MagicMock()
    stub.get_uri = AsyncMock()
    stub.get_store_dir = AsyncMock()
    stub.is_valid_path = AsyncMock()
    stub.parse_store_path = AsyncMock()
    stub.query_path_info = AsyncMock()
    stub.query_path_from_hash_part = AsyncMock()
    stub.compute_fs_closure = AsyncMock()
    stub.query_missing = AsyncMock()
    stub.query_derivation_outputs = AsyncMock()
    stub.query_valid_derivers = AsyncMock()
    stub.query_all_valid_paths = AsyncMock()
    stub.query_referrers = AsyncMock()
    stub.query_substitutable_paths = AsyncMock()
    stub.build_paths_with_results = AsyncMock()
    stub.build_for_humans = AsyncMock()
    stub.read_derivation = AsyncMock()
    stub.build_derivation = AsyncMock()
    stub.follow_links_to_store_path = AsyncMock()
    stub.add_temp_root = AsyncMock()
    stub.find_roots = AsyncMock()
    stub.collect_garbage = AsyncMock()
    stub.add_perm_root = AsyncMock()
    stub.add_indirect_root = AsyncMock()
    stub.ensure_path = AsyncMock()
    stub.optimise_store = AsyncMock()
    stub.verify_store = AsyncMock()
    stub.get_build_log = AsyncMock()
    stub.add_to_store = AsyncMock()
    stub.compute_store_path = AsyncMock()
    stub.fetch_from_url = AsyncMock()
    stub.fetch_from_attrs = AsyncMock()
    return stub


@pytest.fixture
def pool() -> MagicMock:
    p: MagicMock = MagicMock()
    p._store_stub = _make_stub_mock()  # type: ignore[reportPrivateUsage] -- test fixture sets private stub

    async def _passthrough(coro: Any) -> Any:
        return await coro

    p.call = _passthrough
    return p


@pytest.fixture
def store(pool: MagicMock) -> Store:
    s = Store(pool, "mock", "mock-session-id")
    s._active = True  # type: ignore[reportPrivateUsage] -- bypass async open() for mock tests
    return s


# Proto-compatible mock response helpers


def _mock_store_path(to_string: str = "aaa-bbb", hash_part: str = "aaa", name: str = "bbb") -> MagicMock:
    sp = MagicMock()
    sp.to_string = to_string
    sp.hash_part = hash_part
    sp.name = name
    return sp


def _mock_path_info(**overrides: Any) -> MagicMock:
    pi = MagicMock()
    pi.path = overrides.get("path", _mock_store_path())
    pi.nar_hash = overrides.get("nar_hash", "sha256:abc")
    pi.nar_size = overrides.get("nar_size", 1234)
    pi.registration_time = overrides.get("registration_time")
    pi.deriver = overrides.get("deriver")
    pi.references = overrides.get("references", [])
    pi.ca = overrides.get("ca")
    pi.ultimate = overrides.get("ultimate", True)
    return pi


def _mock_build_result(**overrides: Any) -> MagicMock:
    br = MagicMock()
    br.drv_path = overrides.get("drv_path", "")
    br.success = overrides.get("success", True)
    br.status = overrides.get("status", "built")
    br.error_msg = overrides.get("error_msg", "")
    return br


def _mock_derivation(**overrides: Any) -> MagicMock:
    d = MagicMock()
    d.name = overrides.get("name", "foo")
    d.system = overrides.get("system", "x86_64-linux")
    d.builder = overrides.get("builder", "/bin/sh")
    d.args = overrides.get("args", [])
    d.env = overrides.get("env", {})
    d.input_drvs = overrides.get("input_drvs", {})
    d.input_srcs = overrides.get("input_srcs", [])
    return d


def _mock_store_path_list(paths: list[Any] | None = None) -> MagicMock:
    spl = MagicMock()
    spl.paths = paths or []
    return spl


def _mock_build_result_list(results: list[Any] | None = None) -> MagicMock:
    brl = MagicMock()
    brl.results = results or []
    return brl


def _mock_missing_info(**overrides: Any) -> MagicMock:
    mi = MagicMock()
    mi.will_build = overrides.get("will_build", [])
    mi.will_substitute = overrides.get("will_substitute", [])
    mi.unknown = overrides.get("unknown", [])
    mi.download_size = overrides.get("download_size", 0)
    mi.nar_size = overrides.get("nar_size", 0)
    return mi


def _mock_find_roots_response(roots: list[Any] | None = None) -> MagicMock:
    response = MagicMock()
    response.roots = roots or []
    return response


def _mock_collect_garbage_response(**overrides: Any) -> MagicMock:
    response = MagicMock()
    response.paths = overrides.get("paths", [])
    response.bytes_freed = overrides.get("bytes_freed", 0)
    return response


# ════════════════════════════════════════════════════════════════════
# Identity / simple pass-through
# ════════════════════════════════════════════════════════════════════


class TestIdentity:
    async def test_get_uri(self, store: Store, pool: MagicMock):
        pool._store_stub.get_uri.return_value = MagicMock(uri="daemon")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.get_uri(GetUriRequest())
        assert result.uri == "daemon"
        pool._store_stub.get_uri.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_get_store_dir(self, store: Store, pool: MagicMock):
        pool._store_stub.get_store_dir.return_value = MagicMock(dir="/nix/store")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.get_store_dir(GetStoreDirRequest())
        assert result.dir == "/nix/store"
        pool._store_stub.get_store_dir.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_get_build_log(self, store: Store, pool: MagicMock):
        pool._store_stub.get_build_log.return_value = MagicMock(log="hello log\n")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.get_build_log(GetBuildLogRequest(path="/nix/store/aaa-bbb"))
        assert result.log == "hello log\n"
        pool._store_stub.get_build_log.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_add_to_store_injects_store_handle(self, store: Store, pool: MagicMock):
        pool._store_stub.add_to_store.return_value = AddToStoreResponse(path="/nix/store/aaa-added")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        store._store_handle = 123  # type: ignore[reportPrivateUsage] -- test injects store handle directly
        request = AddToStoreRequest(path="/tmp/source", name="source", method="nar", hash_algo="sha256")

        result = await store.add_to_store(request)

        assert result.path == "/nix/store/aaa-added"
        pool._store_stub.add_to_store.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sent = pool._store_stub.add_to_store.await_args.args[0]  # type: ignore[reportPrivateUsage, reportOptionalMemberAccess, reportOptionalSubscript] -- test inspects stub call args; await_args may be None
        assert sent.store_handle == 123

    async def test_compute_store_path_injects_store_handle(self, store: Store, pool: MagicMock):
        pool._store_stub.compute_store_path.return_value = ComputeStorePathResponse(path="/nix/store/bbb-added")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        store._store_handle = 456  # type: ignore[reportPrivateUsage] -- test injects store handle directly
        request = ComputeStorePathRequest(path="/tmp/source", method="flat", hash_algo="sha256")

        result = await store.compute_store_path(request)

        assert result.path == "/nix/store/bbb-added"
        pool._store_stub.compute_store_path.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sent = pool._store_stub.compute_store_path.await_args.args[0]  # type: ignore[reportPrivateUsage, reportOptionalMemberAccess, reportOptionalSubscript] -- test inspects stub call args; await_args may be None
        assert sent.store_handle == 456


class TestPublicStore:
    @pytest.fixture
    def public_store(self, store: Store) -> PublicStore:
        return PublicStore(store)

    async def test_uri_unwraps_response(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.get_uri.return_value = MagicMock(uri="daemon")  # type: ignore[reportPrivateUsage] -- test accesses private stub

        assert await public_store.uri() == "daemon"
        pool._store_stub.get_uri.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_store_dir_unwraps_response(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.get_store_dir.return_value = MagicMock(dir="/nix/store")  # type: ignore[reportPrivateUsage] -- test accesses private stub

        assert await public_store.store_dir() == "/nix/store"
        pool._store_stub.get_store_dir.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_parse_store_path_returns_model(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.parse_store_path.return_value = ParseStorePathResponse(path="/nix/store/aaa-bbb")  # type: ignore[reportPrivateUsage] -- test accesses private stub

        path = await public_store.parse_store_path("/nix/store/aaa-bbb")

        assert path == "/nix/store/aaa-bbb"

    async def test_is_valid_path_unwraps_response(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.is_valid_path.return_value = MagicMock(valid=True)  # type: ignore[reportPrivateUsage] -- test accesses private stub

        assert await public_store.is_valid_path("/nix/store/aaa-bbb")

    async def test_rpc_exposes_generated_surface(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.get_build_log.return_value = MagicMock(log="hello log\n")  # type: ignore[reportPrivateUsage] -- test accesses private stub

        response = await public_store.rpc.get_build_log(GetBuildLogRequest(path="/nix/store/aaa-bbb"))

        assert response.log == "hello log\n"

    async def test_query_missing_sends_request(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.query_missing.return_value = _mock_missing_info(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            will_build=["/nix/store/aaa-foo.drv"],
            will_substitute=["/nix/store/bbb-bar"],
            unknown=[],
            download_size=12345,
            nar_size=67890,
        )
        result = await public_store.query_missing(["/nix/store/aaa-foo.drv", "/nix/store/bbb-bar"])
        assert result.will_build == ["/nix/store/aaa-foo.drv"]
        assert result.download_size == 12345
        pool._store_stub.query_missing.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sent = pool._store_stub.query_missing.await_args.args[0]  # type: ignore[reportPrivateUsage, reportOptionalMemberAccess, reportOptionalSubscript] -- test inspects stub call args
        assert sent.derived_paths == ["/nix/store/aaa-foo.drv", "/nix/store/bbb-bar"]

    async def test_build_paths_with_results_sends_derived_paths(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.build_paths_with_results.return_value = _mock_build_result_list(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            [_mock_build_result(drv_path="/nix/store/aaa-foo.drv", success=True)]
        )

        results = await public_store.build_paths_with_results(["/nix/store/aaa-foo.drv"])

        assert results[0].success
        sent = pool._store_stub.build_paths_with_results.await_args.args[0]  # type: ignore[reportPrivateUsage, reportOptionalMemberAccess, reportOptionalSubscript] -- test inspects stub call args
        assert sent.derived_paths == ["/nix/store/aaa-foo.drv"]
        assert sent.build_mode == 0

    async def test_read_derivation_sends_request(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.read_derivation.return_value = _mock_derivation(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            name="foo", system="x86_64-linux", builder="/bin/sh"
        )
        result = await public_store.read_derivation("/nix/store/aaa-foo.drv")
        assert result.name == "foo"
        assert result.system == "x86_64-linux"
        pool._store_stub.read_derivation.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sent = pool._store_stub.read_derivation.await_args.args[0]  # type: ignore[reportPrivateUsage, reportOptionalMemberAccess, reportOptionalSubscript] -- test inspects stub call args
        assert sent.path == "/nix/store/aaa-foo.drv"

    async def test_collect_garbage_sends_request(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.collect_garbage.return_value = _mock_collect_garbage_response(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            paths=["/nix/store/aaa-foo", "/nix/store/bbb-bar"],
            bytes_freed=4096,
        )
        result = await public_store.collect_garbage(GcAction.RETURN_DEAD)
        assert result.paths == ["/nix/store/aaa-foo", "/nix/store/bbb-bar"]
        assert result.bytes_freed == 4096
        pool._store_stub.collect_garbage.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sent = pool._store_stub.collect_garbage.await_args.args[0]  # type: ignore[reportPrivateUsage, reportOptionalMemberAccess, reportOptionalSubscript] -- test inspects stub call args
        assert sent.action == GcAction.RETURN_DEAD

    async def test_collect_garbage_with_options(self, public_store: PublicStore, pool: MagicMock):
        pool._store_stub.collect_garbage.return_value = _mock_collect_garbage_response(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            paths=[],
            bytes_freed=0,
        )
        result = await public_store.collect_garbage(
            GcAction.DELETE_SPECIFIC,
            ignore_liveness=True,
            paths_to_delete=["/nix/store/aaa-foo"],
            max_freed=1000,
        )
        assert result.paths == []
        pool._store_stub.collect_garbage.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sent = pool._store_stub.collect_garbage.await_args.args[0]  # type: ignore[reportPrivateUsage, reportOptionalMemberAccess, reportOptionalSubscript] -- test inspects stub call args
        assert sent.action == GcAction.DELETE_SPECIFIC
        assert sent.ignore_liveness is True
        assert sent.paths_to_delete == ["/nix/store/aaa-foo"]
        assert sent.max_freed == 1000


# ════════════════════════════════════════════════════════════════════
# StorePath parsing — coercion from StorePath or str
# ════════════════════════════════════════════════════════════════════


class TestStorePathCoercion:
    async def test_parse_store_path_returns_proto(self, store: Store, pool: MagicMock):
        pool._store_stub.parse_store_path.return_value = ParseStorePathResponse(path="/nix/store/aaa-bbb")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.parse_store_path(ParseStorePathRequest(path="/nix/store/aaa-bbb"))
        assert result.path == "/nix/store/aaa-bbb"

    async def test_is_valid_path_accepts_str(self, store: Store, pool: MagicMock):
        pool._store_stub.is_valid_path.return_value = MagicMock(valid=True)  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.is_valid_path(IsValidPathRequest(path="/nix/store/aaa-bbb"))
        assert result.valid is True

    async def test_is_valid_path_accepts_storepath(self, store: Store, pool: MagicMock):
        pool._store_stub.is_valid_path.return_value = MagicMock(valid=True)  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        result = await store.is_valid_path(IsValidPathRequest(path=sp.to_string))
        assert result.valid is True

    async def test_follow_links_returns_storepath(self, store: Store, pool: MagicMock):
        pool._store_stub.follow_links_to_store_path.return_value = FollowLinksToStorePathResponse(path="/nix/store/aaa-bbb")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.follow_links_to_store_path(FollowLinksToStorePathRequest(path="/some/link"))
        assert result.path == "/nix/store/aaa-bbb"


# ════════════════════════════════════════════════════════════════════
# Path info
# ════════════════════════════════════════════════════════════════════


class TestPathInfo:
    async def test_query_path_info_str(self, store: Store, pool: MagicMock):
        pool._store_stub.query_path_info.return_value = _mock_path_info(nar_size=1234)  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.query_path_info(QueryPathInfoRequest(path="/nix/store/aaa-foo"))
        assert result.nar_size == 1234

    async def test_query_path_info_storepath(self, store: Store, pool: MagicMock):
        pool._store_stub.query_path_info.return_value = _mock_path_info()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        await store.query_path_info(QueryPathInfoRequest(path=sp.to_string))

    async def test_query_path_from_hash_part_found(self, store: Store, pool: MagicMock):
        pool._store_stub.query_path_from_hash_part.return_value = MagicMock(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            path="/nix/store/aaa-foo"
        )
        result = await store.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part="aaa"))
        assert result.path is not None
        assert result.path == "/nix/store/aaa-foo"

    async def test_query_path_from_hash_part_not_found(self, store: Store, pool: MagicMock):
        pool._store_stub.query_path_from_hash_part.return_value = MagicMock(path=None)  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part="nonexistent"))
        assert result.path is None


# ════════════════════════════════════════════════════════════════════
# Closures
# ════════════════════════════════════════════════════════════════════


class TestClosures:
    async def test_compute_fs_closure(self, store: Store, pool: MagicMock):
        pool._store_stub.compute_fs_closure.return_value = _mock_store_path_list(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            [_mock_store_path("aaa-foo", "aaa", "foo"), _mock_store_path("bbb-bar", "bbb", "bar")]
        )
        result = await store.compute_fs_closure(ComputeFsClosureRequest(path="/nix/store/aaa-foo", flip_direction=True))
        assert len(result.paths) == 2

    async def test_query_missing_coerces_list(self, store: Store, pool: MagicMock):
        pool._store_stub.query_missing.return_value = _mock_missing_info()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        result = await store.query_missing(QueryMissingRequest(derived_paths=[sp.to_string, "/nix/store/bbb-bar"]))
        assert isinstance(result, MagicMock)


# ════════════════════════════════════════════════════════════════════
# Derivations
# ════════════════════════════════════════════════════════════════════


class TestDerivations:
    async def test_query_derivation_outputs_str(self, store: Store, pool: MagicMock):
        pool._store_stub.query_derivation_outputs.return_value = _mock_store_path_list(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            [_mock_store_path("aaa-out", "aaa", "out")]
        )
        result = await store.query_derivation_outputs(QueryDerivationOutputsRequest(path="/nix/store/aaa-foo.drv"))
        assert len(result.paths) == 1

    async def test_query_valid_derivers_storepath(self, store: Store, pool: MagicMock):
        pool._store_stub.query_valid_derivers.return_value = _mock_store_path_list()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        result = await store.query_valid_derivers(QueryValidDeriversRequest(path=sp.to_string))
        assert result.paths == []


# ════════════════════════════════════════════════════════════════════
# Bulk queries
# ════════════════════════════════════════════════════════════════════


class TestBulk:
    async def test_query_all_valid_paths(self, store: Store, pool: MagicMock):
        pool._store_stub.query_all_valid_paths.return_value = _mock_store_path_list()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.query_all_valid_paths(QueryAllValidPathsRequest())
        assert result.paths == []

    async def test_query_referrers(self, store: Store, pool: MagicMock):
        pool._store_stub.query_referrers.return_value = _mock_store_path_list()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.query_referrers(QueryReferrersRequest(path="/nix/store/aaa-foo"))
        assert result.paths == []

    async def test_query_substitutable_paths_coerces_list(self, store: Store, pool: MagicMock):
        pool._store_stub.query_substitutable_paths.return_value = _mock_store_path_list()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        result = await store.query_substitutable_paths(
            QuerySubstitutablePathsRequest(paths=[sp.to_string, "/nix/store/bbb-bar"])
        )
        assert result.paths == []


# ════════════════════════════════════════════════════════════════════
# Build
# ════════════════════════════════════════════════════════════════════


class TestBuild:
    async def test_build_paths_with_results(self, store: Store, pool: MagicMock):
        pool._store_stub.build_paths_with_results.return_value = _mock_build_result_list(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            [_mock_build_result(drv_path="/nix/store/aaa.drv", success=True, status="built")]
        )
        result = await store.build_paths_with_results(BuildPathsWithResultsRequest(derived_paths=["/nix/store/aaa.drv"]))
        assert len(result.results) == 1
        assert result.results[0].success is True

    async def test_build_for_humans(self, store: Store, pool: MagicMock):
        pool._store_stub.build_for_humans.return_value = _mock_build_result_list(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            [_mock_build_result(drv_path="/nix/store/aaa.drv", success=True, status="substituted")]
        )
        result = await store.build_for_humans(BuildPathsWithResultsRequest(derived_paths=["/nix/store/aaa.drv"]))
        assert len(result.results) == 1
        assert result.results[0].status == "substituted"

    async def test_read_derivation(self, store: Store, pool: MagicMock):
        pool._store_stub.read_derivation.return_value = _mock_derivation(name="foo", system="x86_64-linux")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.read_derivation(ReadDerivationRequest(path="/nix/store/aaa-foo.drv"))
        assert result.name == "foo"
        assert result.system == "x86_64-linux"


# ════════════════════════════════════════════════════════════════════
# GC
# ════════════════════════════════════════════════════════════════════


class TestGC:
    async def test_add_temp_root(self, store: Store, pool: MagicMock):
        pool._store_stub.add_temp_root.return_value = MagicMock()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        await store.add_temp_root(AddTempRootRequest(path="/nix/store/aaa-foo"))
        pool._store_stub.add_temp_root.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_find_roots(self, store: Store, pool: MagicMock):
        pool._store_stub.find_roots.return_value = _mock_find_roots_response()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.find_roots(FindRootsRequest(censor=True))
        assert result.roots == []
        pool._store_stub.find_roots.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_collect_garbage_dry_run(self, store: Store, pool: MagicMock):
        pool._store_stub.collect_garbage.return_value = _mock_collect_garbage_response(  # type: ignore[reportPrivateUsage] -- test accesses private stub
            paths=["/nix/store/aaa-foo"],
            bytes_freed=0,
        )
        result = await store.collect_garbage(CollectGarbageRequest(action=GcAction.RETURN_DEAD))
        assert result.paths == ["/nix/store/aaa-foo"]
        assert result.bytes_freed == 0
        pool._store_stub.collect_garbage.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_add_perm_root(self, store: Store, pool: MagicMock):
        pool._store_stub.add_perm_root.return_value = MagicMock(path="/tmp/root")  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.add_perm_root(AddPermRootRequest(store_path="/nix/store/aaa-foo", gc_root="/tmp/root"))
        assert result.path == "/tmp/root"
        pool._store_stub.add_perm_root.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_add_indirect_root(self, store: Store, pool: MagicMock):
        pool._store_stub.add_indirect_root.return_value = MagicMock()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        await store.add_indirect_root(AddIndirectRootRequest(path="/tmp/root"))
        pool._store_stub.add_indirect_root.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_ensure_path(self, store: Store, pool: MagicMock):
        pool._store_stub.ensure_path.return_value = MagicMock()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        await store.ensure_path(EnsurePathRequest(path="/nix/store/aaa-foo"))
        pool._store_stub.ensure_path.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_optimise_store(self, store: Store, pool: MagicMock):
        pool._store_stub.optimise_store.return_value = MagicMock()  # type: ignore[reportPrivateUsage] -- test accesses private stub
        await store.optimise_store(OptimiseStoreRequest())
        pool._store_stub.optimise_store.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub

    async def test_verify_store(self, store: Store, pool: MagicMock):
        pool._store_stub.verify_store.return_value = MagicMock(errors=False)  # type: ignore[reportPrivateUsage] -- test accesses private stub
        result = await store.verify_store(VerifyStoreRequest(check_contents=False, repair=False))
        assert result.errors is False
        pool._store_stub.verify_store.assert_awaited_once()  # type: ignore[reportPrivateUsage] -- test accesses private stub


# ════════════════════════════════════════════════════════════════════
# Package exports (C3 fix)
# ════════════════════════════════════════════════════════════════════


async def test_public_store_is_distinct_from_rpc_transport():
    """The public facade intentionally separates ergonomic and generated APIs."""
    assert nanopynix.Store is PublicStore
    assert nanopynix.Store is not nanopynix.StoreHandle
