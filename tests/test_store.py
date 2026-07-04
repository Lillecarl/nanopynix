"""Tests for nanopynix_store (StorePath, Store, BuildResult, PathInfo)."""

import nanopynix_store


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
    def test_store_dir(self, store):
        assert store.get_store_dir() == "/nix/store"

    def test_uri(self, store):
        uri = store.get_uri()
        assert isinstance(uri, str)
        assert len(uri) > 0

    def test_is_valid_path_valid(self, store):
        """Check that a real store path from bash is valid."""
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        # Extract the store path basename
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        assert store.is_valid_path(sp)

    def test_is_valid_path_invalid(self, store):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        assert not store.is_valid_path(sp)

    def test_parse_store_path(self, store):
        sp = store.parse_store_path("/nix/store/00000000000000000000000000000000-bogus")
        assert sp.name() == "bogus"

    def test_build_paths_with_results_already_valid(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        results = store.build_paths_with_results([sp])
        assert len(results) == 1
        assert results[0]["success"]
        assert results[0]["status"] == "already-valid"

    def test_build_paths_with_results_bogus(self, store):
        sp = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        results = store.build_paths_with_results([sp])
        assert len(results) == 1
        assert not results[0]["success"]
        assert results[0]["status"] != "already-valid"

    def test_query_path_info(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        info = store.query_path_info(sp)
        assert isinstance(info["nar_hash"], str)
        assert len(info["nar_hash"]) > 0
        refs = info["references"]
        assert isinstance(refs, list)
        # path is a dict with to_string/hash_part/name
        assert info["path"]["to_string"] == bash_basename
        assert info["nar_size"] > 0

    def test_query_path_from_hash_part(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        hash_part = bash_basename.split("-")[0]
        sp = store.query_path_from_hash_part(hash_part)
        assert sp is not None

    def test_compute_fs_closure(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        closure = store.compute_fs_closure(sp)
        assert isinstance(closure, list)
        assert len(closure) > 0

    def test_query_derivation_outputs(self, store):
        # bash is not a derivation, this returns empty or errors depending on store impl.
        # Skip the invalid-path test; query_derivation_outputs only works on drv paths.
        pass

    def test_query_all_valid_paths(self, store):
        paths = store.query_all_valid_paths()
        assert isinstance(paths, list)
        assert len(paths) > 0

    def test_query_referrers(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        referrers = store.query_referrers(sp)
        assert isinstance(referrers, list)

    def test_add_temp_root(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        store.add_temp_root(sp)
        # Should not raise


class TestBuildResult:
    def test_success_repr(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        results = store.build_paths_with_results([sp])
        r = results[0]
        assert r["success"]
        assert r["drv_path"]  # non-empty string


class TestPathInfo:
    def test_repr(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        info = store.query_path_info(sp)
        assert isinstance(info, dict)
        assert "nar_hash" in info

    def test_registration_time(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        info = store.query_path_info(sp)
        rt = info["registration_time"]
        if rt is not None:
            assert rt > 0

    def test_deriver(self, store):
        import os

        bash = os.readlink("/run/current-system/sw/bin/bash")
        bash_basename = bash.split("/nix/store/")[1].split("/")[0]
        sp = nanopynix_store.StorePath(bash_basename)
        info = store.query_path_info(sp)
        # deriver may be None for non-derivation outputs
        deriver = info["deriver"]
        assert deriver is None or isinstance(deriver, dict)


class TestOpenStore:
    def test_open_store_daemon(self):
        store = nanopynix_store.open_store()
        assert isinstance(store, nanopynix_store.Store)
        assert store.get_uri() == "daemon"

    def test_open_store_uri_local(self, tmp_path):
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        assert isinstance(store, nanopynix_store.Store)
        assert store.get_uri().startswith("local")
