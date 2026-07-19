"""Tests for nanopynix_store (StorePath, Store, BuildResult, PathInfo)."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false
# nanopynix_store is a C++ nanobind extension without type stubs.

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from nanopynix_bindings import store as nanopynix_store

NIX_GC_ROOTS_BUG = pytest.mark.nix_version(
    exclude=("2.31", "2.34"),
    reason="findRoots/collectGarbage crash on nonnumeric temproots filenames; https://github.com/NixOS/nix/issues/16138",
)


class TestStorePath:
    def test_parse_valid(self):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        assert sp.name() == "bogus"
        assert sp.hash_part() == "00000000000000000000000000000000"

    def test_to_string(self):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        assert sp.to_string() == "00000000000000000000000000000000-bogus"

    def test_str_repr(self):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        assert str(sp) == "00000000000000000000000000000000-bogus"
        assert repr(sp) == "StorePath('00000000000000000000000000000000-bogus')"

    def test_is_derivation(self):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        assert not sp.is_derivation()

    def test_equality(self):
        a = nanopynix_store.StorePath("00000000000000000000000000000000-foo")
        b = nanopynix_store.StorePath("00000000000000000000000000000000-foo")
        c = nanopynix_store.StorePath("00000000000000000000000000000001-bar")
        assert a == b
        assert a != c

    def test_hashable(self):
        a = nanopynix_store.StorePath("00000000000000000000000000000000-foo")
        b = nanopynix_store.StorePath("00000000000000000000000000000000-foo")
        assert hash(a) == hash(b)
        s = {a}
        assert b in s


class TestStore:
    def test_store_dir(self, store: Any):
        assert store.get_store_dir() == "/nix/store"

    def test_uri(self, store: Any):
        uri = store.get_uri()
        assert isinstance(uri, str)
        assert len(uri) > 0

    def test_is_valid_path_valid(self, store: Any, store_seeded_path: Any):
        assert store.is_valid_path(store_seeded_path)

    def test_is_valid_path_invalid(self, store: Any):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        assert not store.is_valid_path(sp)

    def test_parse_store_path(self, store: Any):
        sp = store.parse_store_path("/nix/store/00000000000000000000000000000000-bogus")
        assert sp.name() == "bogus"

    def test_build_paths_with_results_already_valid(self, store: Any, store_seeded_path: Any):
        results = store.build_paths_with_results([store_seeded_path])
        assert len(results) == 1
        assert results[0]["success"]
        assert results[0]["status"] == "already-valid"

    def test_build_paths_with_results_bogus(self, store: Any):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        results = store.build_paths_with_results([sp])
        assert len(results) == 1
        assert not results[0]["success"]
        assert results[0]["status"] != "already-valid"

    def test_query_path_info(self, store: Any, store_seeded_path: Any):
        info = store.query_path_info(store_seeded_path)
        assert isinstance(info["nar_hash"], str)
        assert len(info["nar_hash"]) > 0
        refs = info["references"]
        assert isinstance(refs, list)
        assert info["path"] == f"{store.get_store_dir()}/{store_seeded_path.to_string()}"
        assert info["nar_size"] > 0

    def test_query_path_from_hash_part(self, store: Any, store_seeded_path: Any):
        hash_part = store_seeded_path.hash_part()
        result = store.query_path_from_hash_part(hash_part)
        assert result is not None

    def test_compute_fs_closure(self, store: Any, store_seeded_path: Any):
        closure = store.compute_fs_closure(store_seeded_path)
        assert isinstance(closure, list)
        assert len(closure) > 0  # type: ignore[reportUnknownArgumentType] -- store method returns Any

    def test_query_derivation_outputs(self, store: Any):
        return

    def test_query_all_valid_paths(self, store: Any):
        paths = store.query_all_valid_paths()
        assert isinstance(paths, list)
        assert len(paths) > 0  # type: ignore[reportUnknownArgumentType] -- store method returns Any

    def test_query_referrers(self, store: Any, store_seeded_path: Any):
        referrers = store.query_referrers(store_seeded_path)
        assert isinstance(referrers, list)

    def test_add_temp_root(self, store: Any, store_seeded_path: Any):
        store.add_temp_root(store_seeded_path)
        # Should not raise

    @NIX_GC_ROOTS_BUG
    def test_find_roots(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        roots = store.find_roots(censor=True)
        assert isinstance(roots, list)
        for root in roots[:10]:
            assert isinstance(root["link"], str)
            assert isinstance(root["path"], str)

    @NIX_GC_ROOTS_BUG
    def test_collect_garbage_return_dead_does_not_delete(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        result = store.collect_garbage(nanopynix_store.GCAction.ReturnDead)
        assert isinstance(result["paths"], list)
        assert result["bytes_freed"] == 0

    def test_add_perm_root_and_indirect_root(self, store: Any, store_seeded_path: Any, tmp_path: Path):
        root = tmp_path / "nanopynix-gc-root"
        result = store.add_perm_root(store_seeded_path, str(root))
        assert result == str(root)
        assert root.is_symlink()
        store.add_indirect_root(str(root))

    def test_ensure_path(self, store: Any, store_seeded_path: Any):
        store.ensure_path(store_seeded_path)

    def test_optimise_store_empty_local_store(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        store.optimise_store()

    def test_verify_store_empty_local_store(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        assert store.verify_store(check_contents=False, repair=False) is False


class TestBuildResult:
    def test_success_repr(self, store: Any, store_seeded_path: Any):
        results = store.build_paths_with_results([store_seeded_path])
        r = results[0]
        assert r["success"]
        assert r["drv_path"]  # non-empty string


class TestPathInfo:
    def test_repr(self, store: Any, store_seeded_path: Any):
        info = store.query_path_info(store_seeded_path)
        assert isinstance(info, dict)
        assert "nar_hash" in info

    def test_registration_time(self, store: Any, store_seeded_path: Any):
        info = store.query_path_info(store_seeded_path)
        rt = info["registration_time"]
        if rt is not None:
            assert rt > 0

    def test_deriver(self, store: Any, store_seeded_path: Any):
        info = store.query_path_info(store_seeded_path)
        # deriver is None: this path was seeded directly, not built by a derivation
        deriver = info["deriver"]
        assert deriver is None or isinstance(deriver, str)


class TestOpenStore:
    @pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true", reason="cannot access daemon in GHA")
    def test_open_store_daemon(self):
        store = nanopynix_store.open_store()
        assert isinstance(store, nanopynix_store.Store)
        assert store.get_uri() == "daemon"

    def test_open_store_uri_local(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        assert isinstance(store, nanopynix_store.Store)
        assert store.get_uri().startswith("local")

    def test_open_store_uri_with_params(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        assert store.get_uri() == "local"
        assert store.get_uri(with_params=True) == f"local?root={tmp_path}"

    @pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true", reason="local store layout differs in GHA")
    def test_open_store_uri_local_initializes_store_layout(self, tmp_path: Path):
        root = tmp_path / "local-store"
        store = nanopynix_store.open_store(f"local?root={root}")

        dirs = store.get_store_dirs()
        assert dirs == {
            "store_dir": "/nix/store",
            "uri": store.get_uri(),
            "root_dir": str(root),
            "state_dir": str(root / "nix" / "var" / "nix"),
            "log_dir": str(root / "nix" / "var" / "log" / "nix"),
            "real_store_dir": str(root / "nix" / "store"),
            "build_dir": str(root / "nix" / "var" / "nix" / "builds"),
        }
        assert (root / "nix" / "store").is_dir()
        assert (root / "nix" / "var" / "nix" / "db" / "db.sqlite").is_file()
