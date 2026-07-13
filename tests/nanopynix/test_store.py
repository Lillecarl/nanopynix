"""Tests for nanopynix_store (StorePath, Store, BuildResult, PathInfo)."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# nanopynix_store is a C++ nanobind extension without type stubs.

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import nanopynix_store


def _bash_sp() -> nanopynix_store.StorePath:
    """Return a StorePath for the system bash binary.  Requires NixOS."""
    bash = os.readlink("/run/current-system/sw/bin/bash")  # noqa: PTH115
    bash_basename = bash.split("/nix/store/")[1].split("/")[0]
    return nanopynix_store.StorePath(bash_basename)


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

    def test_is_valid_path_valid(self, store: Any):
        sp = _bash_sp()
        assert store.is_valid_path(sp)

    def test_is_valid_path_invalid(self, store: Any):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        assert not store.is_valid_path(sp)

    def test_parse_store_path(self, store: Any):
        sp = store.parse_store_path("/nix/store/00000000000000000000000000000000-bogus")
        assert sp.name() == "bogus"

    def test_build_paths_with_results_already_valid(self, store: Any):
        sp = _bash_sp()
        results = store.build_paths_with_results([sp])
        assert len(results) == 1
        assert results[0]["success"]
        assert results[0]["status"] == "already-valid"

    def test_build_paths_with_results_bogus(self, store: Any):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        results = store.build_paths_with_results([sp])
        assert len(results) == 1
        assert not results[0]["success"]
        assert results[0]["status"] != "already-valid"

    def test_query_path_info(self, store: Any):
        sp = _bash_sp()
        info = store.query_path_info(sp)
        assert isinstance(info["nar_hash"], str)
        assert len(info["nar_hash"]) > 0
        refs = info["references"]
        assert isinstance(refs, list)
        # path is a proto-shaped dict with only the stored StorePath basename.
        assert info["path"]["base_name"] == sp.to_string()
        assert info["nar_size"] > 0

    def test_query_path_from_hash_part(self, store: Any):
        sp = _bash_sp()
        hash_part = sp.hash_part()
        result = store.query_path_from_hash_part(hash_part)
        assert result is not None

    def test_compute_fs_closure(self, store: Any):
        sp = _bash_sp()
        closure = store.compute_fs_closure(sp)
        assert isinstance(closure, list)
        assert len(closure) > 0

    def test_query_derivation_outputs(self, store: Any):
        return

    def test_query_all_valid_paths(self, store: Any):
        paths = store.query_all_valid_paths()
        assert isinstance(paths, list)
        assert len(paths) > 0

    def test_query_referrers(self, store: Any):
        sp = _bash_sp()
        referrers = store.query_referrers(sp)
        assert isinstance(referrers, list)

    def test_add_temp_root(self, store: Any):
        sp = _bash_sp()
        store.add_temp_root(sp)
        # Should not raise

    def test_find_roots(self, store: Any):
        roots = store.find_roots(censor=True)
        assert isinstance(roots, list)
        for root in roots[:10]:
            assert isinstance(root["link"], str)
            assert isinstance(root["path"], dict)

    def test_collect_garbage_return_dead_does_not_delete(self, store: Any):
        result = store.collect_garbage(nanopynix_store.GCAction.ReturnDead)
        assert isinstance(result["paths"], list)
        assert result["bytes_freed"] == 0

    def test_add_perm_root_and_indirect_root(self, store: Any, tmp_path: Path):
        sp = _bash_sp()
        root = tmp_path / "nanopynix-gc-root"
        result = store.add_perm_root(sp, str(root))
        assert result == str(root)
        assert root.is_symlink()
        store.add_indirect_root(str(root))

    def test_ensure_path(self, store: Any):
        store.ensure_path(_bash_sp())

    def test_optimise_store_empty_local_store(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        store.optimise_store()

    def test_verify_store_empty_local_store(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        assert store.verify_store(check_contents=False, repair=False) is False


class TestBuildResult:
    def test_success_repr(self, store: Any):
        sp = _bash_sp()
        results = store.build_paths_with_results([sp])
        r = results[0]
        assert r["success"]
        assert r["drv_path"]  # non-empty string


class TestPathInfo:
    def test_repr(self, store: Any):
        sp = _bash_sp()
        info = store.query_path_info(sp)
        assert isinstance(info, dict)
        assert "nar_hash" in info

    def test_registration_time(self, store: Any):
        sp = _bash_sp()
        info = store.query_path_info(sp)
        rt = info["registration_time"]
        if rt is not None:
            assert rt > 0

    def test_deriver(self, store: Any):
        sp = _bash_sp()
        info = store.query_path_info(sp)
        # deriver may be None for non-derivation outputs
        deriver = info["deriver"]
        assert deriver is None or isinstance(deriver, dict)


class TestOpenStore:
    def test_open_store_daemon(self):
        store = nanopynix_store.open_store()
        assert isinstance(store, nanopynix_store.Store)
        assert store.get_uri() == "daemon"

    def test_open_store_uri_local(self, tmp_path: Path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        assert isinstance(store, nanopynix_store.Store)
        assert store.get_uri().startswith("local")
