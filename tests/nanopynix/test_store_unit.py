"""Unit tests for Store facade — mock WorkerPool, validate coercion logic.

No Nix daemon needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from nanopynix_proto.nix.store import (
    AddIndirectRootRequest,
    AddPermRootRequest,
    AddTempRootRequest,
    BuildPathsWithResultsRequest,
    CollectGarbageRequest,
    ComputeFsClosureRequest,
    EnsurePathRequest,
    FindRootsRequest,
    FollowLinksToStorePathRequest,
    GcAction,
    GetStoreDirRequest,
    GetUriRequest,
    IsValidPathRequest,
    OptimiseStoreRequest,
    ParseStorePathRequest,
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

from nanopynix.store import StoreHandle as Store


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
    stub.fetch_from_url = AsyncMock()
    stub.fetch_from_attrs = AsyncMock()
    return stub


@pytest.fixture
def pool() -> MagicMock:
    p: MagicMock = MagicMock()
    p._store_stub = _make_stub_mock()

    async def _passthrough(coro):
        return await coro

    p.call = _passthrough
    return p


@pytest.fixture
def store(pool):
    s = Store(pool, "mock", "mock-session-id")
    s._active = True  # bypass async open() for mock tests
    return s


# Proto-compatible mock response helpers


def _mock_store_path(to_string="aaa-bbb", hash_part="aaa", name="bbb"):
    sp = MagicMock()
    sp.to_string = to_string
    sp.hash_part = hash_part
    sp.name = name
    return sp


def _mock_path_info(**overrides):
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


def _mock_build_result(**overrides):
    br = MagicMock()
    br.drv_path = overrides.get("drv_path", "")
    br.success = overrides.get("success", True)
    br.status = overrides.get("status", "built")
    br.error_msg = overrides.get("error_msg", "")
    return br


def _mock_derivation(**overrides):
    d = MagicMock()
    d.name = overrides.get("name", "foo")
    d.system = overrides.get("system", "x86_64-linux")
    d.builder = overrides.get("builder", "/bin/sh")
    d.args = overrides.get("args", [])
    d.env = overrides.get("env", {})
    d.input_drvs = overrides.get("input_drvs", {})
    d.input_srcs = overrides.get("input_srcs", [])
    return d


def _mock_store_path_list(paths=None):
    spl = MagicMock()
    spl.paths = paths or []
    return spl


def _mock_build_result_list(results=None):
    brl = MagicMock()
    brl.results = results or []
    return brl


def _mock_missing_info(**overrides):
    mi = MagicMock()
    mi.will_build = overrides.get("will_build", [])
    mi.will_substitute = overrides.get("will_substitute", [])
    mi.unknown = overrides.get("unknown", [])
    mi.download_size = overrides.get("download_size", 0)
    mi.nar_size = overrides.get("nar_size", 0)
    return mi


def _mock_find_roots_response(roots=None):
    response = MagicMock()
    response.roots = roots or []
    return response


def _mock_collect_garbage_response(**overrides):
    response = MagicMock()
    response.paths = overrides.get("paths", [])
    response.bytes_freed = overrides.get("bytes_freed", 0)
    return response


# ════════════════════════════════════════════════════════════════════
# Identity / simple pass-through
# ════════════════════════════════════════════════════════════════════


class TestIdentity:
    async def test_get_uri(self, store, pool):
        pool._store_stub.get_uri.return_value = MagicMock(uri="daemon")
        result = await store.get_uri(GetUriRequest())
        assert result.uri == "daemon"
        pool._store_stub.get_uri.assert_awaited_once()

    async def test_get_store_dir(self, store, pool):
        pool._store_stub.get_store_dir.return_value = MagicMock(dir="/nix/store")
        result = await store.get_store_dir(GetStoreDirRequest())
        assert result.dir == "/nix/store"
        pool._store_stub.get_store_dir.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════
# StorePath parsing — coercion from StorePath or str
# ════════════════════════════════════════════════════════════════════


class TestStorePathCoercion:
    async def test_parse_store_path_returns_proto(self, store, pool):
        pool._store_stub.parse_store_path.return_value = _mock_store_path("aaa-bbb", "aaa", "bbb")
        result = await store.parse_store_path(ParseStorePathRequest(path="/nix/store/aaa-bbb"))
        assert result.to_string == "aaa-bbb"

    async def test_is_valid_path_accepts_str(self, store, pool):
        pool._store_stub.is_valid_path.return_value = MagicMock(valid=True)
        result = await store.is_valid_path(IsValidPathRequest(path="/nix/store/aaa-bbb"))
        assert result.valid is True

    async def test_is_valid_path_accepts_storepath(self, store, pool):
        pool._store_stub.is_valid_path.return_value = MagicMock(valid=True)
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        result = await store.is_valid_path(IsValidPathRequest(path=sp.to_string))
        assert result.valid is True

    async def test_follow_links_returns_storepath(self, store, pool):
        pool._store_stub.follow_links_to_store_path.return_value = _mock_store_path("aaa-bbb", "aaa", "bbb")
        result = await store.follow_links_to_store_path(FollowLinksToStorePathRequest(path="/some/link"))
        assert result.to_string == "aaa-bbb"


# ════════════════════════════════════════════════════════════════════
# Path info
# ════════════════════════════════════════════════════════════════════


class TestPathInfo:
    async def test_query_path_info_str(self, store, pool):
        pool._store_stub.query_path_info.return_value = _mock_path_info(nar_size=1234)
        result = await store.query_path_info(QueryPathInfoRequest(path="/nix/store/aaa-foo"))
        assert result.nar_size == 1234

    async def test_query_path_info_storepath(self, store, pool):
        pool._store_stub.query_path_info.return_value = _mock_path_info()
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        await store.query_path_info(QueryPathInfoRequest(path=sp.to_string))

    async def test_query_path_from_hash_part_found(self, store, pool):
        pool._store_stub.query_path_from_hash_part.return_value = MagicMock(
            path=_mock_store_path("aaa-foo", "aaa", "foo")
        )
        result = await store.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part="aaa"))
        assert result.path is not None
        assert result.path.to_string == "aaa-foo"

    async def test_query_path_from_hash_part_not_found(self, store, pool):
        pool._store_stub.query_path_from_hash_part.return_value = MagicMock(path=None)
        result = await store.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part="nonexistent"))
        assert result.path is None


# ════════════════════════════════════════════════════════════════════
# Closures
# ════════════════════════════════════════════════════════════════════


class TestClosures:
    async def test_compute_fs_closure(self, store, pool):
        pool._store_stub.compute_fs_closure.return_value = _mock_store_path_list(
            [_mock_store_path("aaa-foo", "aaa", "foo"), _mock_store_path("bbb-bar", "bbb", "bar")]
        )
        result = await store.compute_fs_closure(ComputeFsClosureRequest(path="/nix/store/aaa-foo", flip_direction=True))
        assert len(result.paths) == 2

    async def test_query_missing_coerces_list(self, store, pool):
        pool._store_stub.query_missing.return_value = _mock_missing_info()
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        result = await store.query_missing(QueryMissingRequest(paths=[sp.to_string, "/nix/store/bbb-bar"]))
        assert isinstance(result, MagicMock)


# ════════════════════════════════════════════════════════════════════
# Derivations
# ════════════════════════════════════════════════════════════════════


class TestDerivations:
    async def test_query_derivation_outputs_str(self, store, pool):
        pool._store_stub.query_derivation_outputs.return_value = _mock_store_path_list(
            [_mock_store_path("aaa-out", "aaa", "out")]
        )
        result = await store.query_derivation_outputs(QueryDerivationOutputsRequest(path="/nix/store/aaa-foo.drv"))
        assert len(result.paths) == 1

    async def test_query_valid_derivers_storepath(self, store, pool):
        pool._store_stub.query_valid_derivers.return_value = _mock_store_path_list()
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        result = await store.query_valid_derivers(QueryValidDeriversRequest(path=sp.to_string))
        assert result.paths == []


# ════════════════════════════════════════════════════════════════════
# Bulk queries
# ════════════════════════════════════════════════════════════════════


class TestBulk:
    async def test_query_all_valid_paths(self, store, pool):
        pool._store_stub.query_all_valid_paths.return_value = _mock_store_path_list()
        result = await store.query_all_valid_paths(QueryAllValidPathsRequest())
        assert result.paths == []

    async def test_query_referrers(self, store, pool):
        pool._store_stub.query_referrers.return_value = _mock_store_path_list()
        result = await store.query_referrers(QueryReferrersRequest(path="/nix/store/aaa-foo"))
        assert result.paths == []

    async def test_query_substitutable_paths_coerces_list(self, store, pool):
        pool._store_stub.query_substitutable_paths.return_value = _mock_store_path_list()
        sp = _mock_store_path("a" * 32 + "-foo", "a" * 32, "foo")
        result = await store.query_substitutable_paths(
            QuerySubstitutablePathsRequest(paths=[sp.to_string, "/nix/store/bbb-bar"])
        )
        assert result.paths == []


# ════════════════════════════════════════════════════════════════════
# Build
# ════════════════════════════════════════════════════════════════════


class TestBuild:
    async def test_build_paths_with_results(self, store, pool):
        pool._store_stub.build_paths_with_results.return_value = _mock_build_result_list(
            [_mock_build_result(drv_path="/nix/store/aaa.drv", success=True, status="built")]
        )
        result = await store.build_paths_with_results(BuildPathsWithResultsRequest(paths=["/nix/store/aaa.drv"]))
        assert len(result.results) == 1
        assert result.results[0].success is True

    async def test_read_derivation(self, store, pool):
        pool._store_stub.read_derivation.return_value = _mock_derivation(name="foo", system="x86_64-linux")
        result = await store.read_derivation(ReadDerivationRequest(path="/nix/store/aaa-foo.drv"))
        assert result.name == "foo"
        assert result.system == "x86_64-linux"


# ════════════════════════════════════════════════════════════════════
# GC
# ════════════════════════════════════════════════════════════════════


class TestGC:
    async def test_add_temp_root(self, store, pool):
        pool._store_stub.add_temp_root.return_value = MagicMock()
        await store.add_temp_root(AddTempRootRequest(path="/nix/store/aaa-foo"))
        pool._store_stub.add_temp_root.assert_awaited_once()

    async def test_find_roots(self, store, pool):
        pool._store_stub.find_roots.return_value = _mock_find_roots_response()
        result = await store.find_roots(FindRootsRequest(censor=True))
        assert result.roots == []
        pool._store_stub.find_roots.assert_awaited_once()

    async def test_collect_garbage_dry_run(self, store, pool):
        pool._store_stub.collect_garbage.return_value = _mock_collect_garbage_response(
            paths=["/nix/store/aaa-foo"],
            bytes_freed=0,
        )
        result = await store.collect_garbage(CollectGarbageRequest(action=GcAction.RETURN_DEAD))
        assert result.paths == ["/nix/store/aaa-foo"]
        assert result.bytes_freed == 0
        pool._store_stub.collect_garbage.assert_awaited_once()

    async def test_add_perm_root(self, store, pool):
        pool._store_stub.add_perm_root.return_value = MagicMock(path="/tmp/root")
        result = await store.add_perm_root(AddPermRootRequest(store_path="/nix/store/aaa-foo", gc_root="/tmp/root"))
        assert result.path == "/tmp/root"
        pool._store_stub.add_perm_root.assert_awaited_once()

    async def test_add_indirect_root(self, store, pool):
        pool._store_stub.add_indirect_root.return_value = MagicMock()
        await store.add_indirect_root(AddIndirectRootRequest(path="/tmp/root"))
        pool._store_stub.add_indirect_root.assert_awaited_once()

    async def test_ensure_path(self, store, pool):
        pool._store_stub.ensure_path.return_value = MagicMock()
        await store.ensure_path(EnsurePathRequest(path="/nix/store/aaa-foo"))
        pool._store_stub.ensure_path.assert_awaited_once()

    async def test_optimise_store(self, store, pool):
        pool._store_stub.optimise_store.return_value = MagicMock()
        await store.optimise_store(OptimiseStoreRequest())
        pool._store_stub.optimise_store.assert_awaited_once()

    async def test_verify_store(self, store, pool):
        pool._store_stub.verify_store.return_value = MagicMock(errors=False)
        result = await store.verify_store(VerifyStoreRequest(check_contents=False, repair=False))
        assert result.errors is False
        pool._store_stub.verify_store.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════
# Package exports (C3 fix)
# ════════════════════════════════════════════════════════════════════


async def test_store_alias_is_bound():
    """nanopynix.Store is a backward-compatible alias for StoreHandle."""
    import nanopynix

    assert nanopynix.Store is nanopynix.StoreHandle
