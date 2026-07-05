"""Unit tests for Store facade — mock WorkerPool, validate coercion logic.

No Nix daemon needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from nanopynix.models import BuildResult, MissingInfo, PathInfo, StorePath
from nanopynix.store import StoreHandle as Store

pytestmark = pytest.mark.asyncio


@pytest.fixture
def pool():
    p = MagicMock()
    p.call = AsyncMock()
    return p

@pytest.fixture
def store(pool):
    s = Store(pool, "mock", "mock-session-id")
    s._active = True  # bypass async open() for mock tests
    return s


# ════════════════════════════════════════════════════════════════════
# Identity / simple pass-through
# ════════════════════════════════════════════════════════════════════

class TestIdentity:
    async def test_get_uri(self, store, pool):
        pool.call.return_value = "daemon"
        result = await store.get_uri()
        assert result == "daemon"
        pool.call.assert_awaited_with("store", "get_uri", [], capture=False)

    async def test_get_store_dir(self, store, pool):
        pool.call.return_value = "/nix/store"
        result = await store.get_store_dir()
        assert result == "/nix/store"
        pool.call.assert_awaited_with("store", "get_store_dir", [], capture=False)


# ════════════════════════════════════════════════════════════════════
# StorePath parsing — coercion from StorePath or str
# ════════════════════════════════════════════════════════════════════

class TestStorePathCoercion:
    async def test_parse_store_path_returns_model(self, store, pool):
        pool.call.return_value = {"to_string": "aaa-bbb", "hash_part": "aaa", "name": "bbb"}
        result = await store.parse_store_path("/nix/store/aaa-bbb")
        assert isinstance(result, StorePath)
        assert str(result.to_string) == "aaa-bbb"

    async def test_is_valid_path_accepts_str(self, store, pool):
        pool.call.return_value = True
        result = await store.is_valid_path("/nix/store/aaa-bbb")
        assert result is True
        pool.call.assert_awaited_with("store", "is_valid_path", ["/nix/store/aaa-bbb"], capture=False)

    async def test_is_valid_path_accepts_storepath(self, store, pool):
        pool.call.return_value = True
        sp = StorePath(hash_part="a" * 32, name="foo", to_string="a" * 32 + "-foo")
        result = await store.is_valid_path(sp)
        assert result is True
        pool.call.assert_awaited_with("store", "is_valid_path", ["a" * 32 + "-foo"], capture=False)

    async def test_follow_links_returns_storepath(self, store, pool):
        pool.call.return_value = {"to_string": "aaa-bbb", "hash_part": "aaa", "name": "bbb"}
        result = await store.follow_links_to_store_path("/some/link")
        assert isinstance(result, StorePath)


# ════════════════════════════════════════════════════════════════════
# Path info
# ════════════════════════════════════════════════════════════════════

class TestPathInfo:
    async def test_query_path_info_str(self, store, pool):
        pool.call.return_value = {
            "path": {"to_string": "aaa-foo", "hash_part": "aaa", "name": "foo"},
            "nar_hash": "sha256:abc",
            "nar_size": 1234,
            "registration_time": None,
            "deriver": None,
            "references": [],
            "ca": None,
            "ultimate": True,
        }
        result = await store.query_path_info("/nix/store/aaa-foo")
        assert isinstance(result, PathInfo)
        assert result.nar_size == 1234
        pool.call.assert_awaited_with("store", "query_path_info", ["/nix/store/aaa-foo"], capture=False)

    async def test_query_path_info_storepath(self, store, pool):
        pool.call.return_value = {
            "path": {"to_string": "aaa-foo", "hash_part": "aaa", "name": "foo"},
            "nar_hash": "sha256:abc",
            "nar_size": 0,
            "registration_time": None,
            "deriver": None,
            "references": [],
            "ca": None,
            "ultimate": False,
        }
        sp = StorePath(hash_part="a" * 32, name="foo", to_string="a" * 32 + "-foo")
        result = await store.query_path_info(sp)
        pool.call.assert_awaited_with("store", "query_path_info", ["a" * 32 + "-foo"], capture=False)


# ════════════════════════════════════════════════════════════════════
# Closures
# ════════════════════════════════════════════════════════════════════

class TestClosures:
    async def test_compute_fs_closure(self, store, pool):
        pool.call.return_value = [
            {"to_string": "aaa-foo", "hash_part": "aaa", "name": "foo"},
            {"to_string": "bbb-bar", "hash_part": "bbb", "name": "bar"},
        ]
        result = await store.compute_fs_closure("/nix/store/aaa-foo", flip=True)
        assert len(result) == 2
        assert all(isinstance(sp, StorePath) for sp in result)
        pool.call.assert_awaited_with(
            "store", "compute_fs_closure",
            ["/nix/store/aaa-foo", True, False, False], capture=False,
        )

    async def test_query_missing_coerces_list(self, store, pool):
        pool.call.return_value = {
            "will_build": [],
            "will_substitute": [],
            "unknown": [],
            "download_size": 0,
            "nar_size": 0,
        }
        sp = StorePath(hash_part="a" * 32, name="foo", to_string="a" * 32 + "-foo")
        result = await store.query_missing([sp, "/nix/store/bbb-bar"])
        assert isinstance(result, MissingInfo)
        pool.call.assert_awaited_with(
            "store", "query_missing",
            [["a" * 32 + "-foo", "/nix/store/bbb-bar"]], capture=False,
        )


# ════════════════════════════════════════════════════════════════════
# Derivations
# ════════════════════════════════════════════════════════════════════

class TestDerivations:
    async def test_query_derivation_outputs_str(self, store, pool):
        pool.call.return_value = [
            {"to_string": "aaa-out", "hash_part": "aaa", "name": "out"},
        ]
        result = await store.query_derivation_outputs("/nix/store/aaa-foo.drv")
        assert len(result) == 1
        assert isinstance(result[0], StorePath)

    async def test_query_valid_derivers_storepath(self, store, pool):
        pool.call.return_value = []
        sp = StorePath(hash_part="a" * 32, name="foo", to_string="a" * 32 + "-foo")
        result = await store.query_valid_derivers(sp)
        assert result == []


# ════════════════════════════════════════════════════════════════════
# Bulk queries
# ════════════════════════════════════════════════════════════════════

class TestBulk:
    async def test_query_all_valid_paths(self, store, pool):
        pool.call.return_value = []
        result = await store.query_all_valid_paths()
        assert result == []

    async def test_query_referrers(self, store, pool):
        pool.call.return_value = []
        result = await store.query_referrers("/nix/store/aaa-foo")
        assert result == []

    async def test_query_substitutable_paths_coerces_list(self, store, pool):
        pool.call.return_value = []
        sp = StorePath(hash_part="a" * 32, name="foo", to_string="a" * 32 + "-foo")
        result = await store.query_substitutable_paths([sp, "/nix/store/bbb-bar"])
        assert result == []
        pool.call.assert_awaited_with(
            "store", "query_substitutable_paths",
            [["a" * 32 + "-foo", "/nix/store/bbb-bar"]], capture=False,
        )


# ════════════════════════════════════════════════════════════════════
# Build
# ════════════════════════════════════════════════════════════════════

class TestBuild:
    async def test_build_paths_with_results(self, store, pool):
        pool.call.return_value = [
            {"drv_path": "/nix/store/aaa.drv", "success": True, "status": "built", "error_msg": ""},
        ]
        result = await store.build_paths_with_results(["/nix/store/aaa.drv"])
        assert len(result) == 1
        assert isinstance(result[0], BuildResult)
        assert result[0].success is True

    async def test_read_derivation(self, store, pool):
        pool.call.return_value = {
            "name": "foo", "platform": "x86_64-linux", "builder": "/bin/sh",
            "args": [], "env": [], "outputs": {}, "inputSrcs": [], "inputDrvs": [],
        }
        result = await store.read_derivation("/nix/store/aaa-foo.drv")
        assert isinstance(result, dict)
        assert result["name"] == "foo"


# ════════════════════════════════════════════════════════════════════
# GC
# ════════════════════════════════════════════════════════════════════

class TestGC:
    async def test_add_temp_root(self, store, pool):
        pool.call.return_value = None
        await store.add_temp_root("/nix/store/aaa-foo")
        pool.call.assert_awaited_with("store", "add_temp_root", ["/nix/store/aaa-foo"], capture=False)


# ════════════════════════════════════════════════════════════════════
# Package exports (C3 fix)
# ════════════════════════════════════════════════════════════════════

async def test_store_alias_is_bound():
    """nanopynix.Store is a backward-compatible alias for StoreHandle."""
    import nanopynix
    assert nanopynix.Store is nanopynix.StoreHandle
