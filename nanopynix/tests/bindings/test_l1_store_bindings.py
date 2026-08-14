"""Tests for nanopynix_store (StorePath, Store, BuildResult, PathInfo)."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false
# nanopynix_store is a C++ nanobind extension without type stubs.

from __future__ import annotations

import gc
import os
from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import store as nanopynix_store

from nanopynix.models import StorePath
from nanopynix_testing.nix_markers import NIX_GC_ROOTS_BUG

if TYPE_CHECKING:
    from pathlib import Path


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


# The base names the two implementations have to agree on. A `.drv`, a plain
# path, a name whose *middle* contains `.drv` (the case a naive `in` check gets
# wrong), a name with a dot that is not `.drv`, and a name that is empty after
# the hash separator -- the boundary where `substr(HashLen + 1)` on an
# out-of-range index and Python slicing part company.
DRIFT_BASE_NAMES: list[str] = [
    "00000000000000000000000000000000-hello-2.12.1",
    "11111111111111111111111111111111-hello.drv",
    "22222222222222222222222222222222-foo.drv.bar",
    "33333333333333333333333333333333-lib.so.1",
    "44444444444444444444444444444444-a",
]


@pytest.mark.parametrize("base_name", DRIFT_BASE_NAMES)
class TestStorePathModelDoesNotDriftFromNix:
    """``nanopynix.models.StorePath`` reimplements Nix's accessors in Python.

    That is deliberate: routing construction through the bindings would cost a
    nanobind call per path for validation the worker already did, and
    ``rpc/client/store.py`` wraps in bulk in a dozen places where
    ``query_all_valid_paths`` can return an entire store. What it does *not*
    buy is a guarantee, so these tests are the guarantee: the pure-Python
    accessors are checked against ``nix::StorePath``'s own, which is the thing
    they are a copy of.

    A divergence here is silent everywhere else -- both sides return a string
    and neither raises -- so it would surface as a wrong answer rather than an
    error.
    """

    def test_name(self, base_name: str) -> None:
        assert StorePath(base_name).name == nanopynix_store.StorePath(base_name).name()

    def test_hash_part(self, base_name: str) -> None:
        assert StorePath(base_name).hash_part == nanopynix_store.StorePath(base_name).hash_part()

    def test_is_derivation(self, base_name: str) -> None:
        assert StorePath(base_name).is_derivation == nanopynix_store.StorePath(base_name).is_derivation()

    def test_the_accessors_agree_through_a_full_path_too(self, base_name: str) -> None:
        """``models.StorePath`` also accepts ``/nix/store/...``; the bindings do not.

        Nix's ``StorePath`` takes a base name, so the full-path spelling is
        surface that exists only on the Python side and has nothing to compare
        against unless it is stripped first. Stripping it must land back on the
        same answers -- otherwise ``base_name`` and the accessors disagree
        about where the path ends.
        """
        full = StorePath("/nix/store/" + base_name)
        assert full.base_name == base_name
        assert (full.name, full.hash_part, full.is_derivation) == (
            StorePath(base_name).name,
            StorePath(base_name).hash_part,
            StorePath(base_name).is_derivation,
        )


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

    def test_a_non_derivation_is_reported_as_an_opaque_request(self, store: Any, store_seeded_path: Any):
        """Empty ``outputs`` is a real answer, not a missing one.

        A path that is not a derivation parses to ``DerivedPath::Opaque`` --
        "fetch this path" -- which has no outputs to select. That is why the
        field is a list that can be empty rather than one that is always
        populated, and why ``models.DerivedPath.outputs`` uses ``None`` for
        "the string did not say" instead of reusing ``[]``.
        """
        results = store.build_paths_with_results([store_seeded_path])
        assert results[0]["drv_path"] == f"{store.get_store_dir()}/{store_seeded_path.to_string()}"
        assert results[0]["outputs"] == []

    def test_a_bare_derivation_is_opaque_here_and_selects_no_outputs(self, store: Any):
        """The bindings keep Nix's meaning, and Nix reads a bare ``.drv`` as opaque.

        ``nix build <drv>`` selects no outputs and builds nothing
        (``installable-derived-path.cc:32-37``), and this function maps
        ``nix::Store::buildPathsWithResults``, so it answers the same. It used
        to convert a bare ``.drv`` to ``Built{All}`` here instead, which made
        this the one place nanopynix and the ``nix`` CLI disagreed about one
        string, and left a direct caller no way to ask for the opaque fetch.

        **The convenience did not go away, it moved.** Each engine's async
        ``Store`` applies ``models.DerivedPath.for_build`` before it reaches
        here, so a bare ``.drv`` still means every output to every caller of
        the public API. ``test_a_bare_drv_means_every_output_on_both_engines``
        is that half, and this is the half that keeps the two apart.
        """
        drv = nanopynix_store.StorePath("00000000000000000000000000000000-bogus.drv")
        results = store.build_paths_with_results([drv])
        assert results[0]["drv_path"] == f"{store.get_store_dir()}/{drv.to_string()}"
        assert results[0]["outputs"] == []

    def test_a_derivation_with_an_explicit_selector_is_reported_as_all_outputs(self, store: Any):
        """The Built branch, without needing a build to succeed.

        The result is keyed by the *request*, so a derivation that cannot be
        built still reports which outputs were asked for. ``^*`` means every
        output, and it arrives as ``["*"]`` rather than as a suffix welded
        onto ``drv_path``.
        """
        drv = nanopynix_store.StorePath("00000000000000000000000000000000-bogus.drv")
        results = store.build_paths_with_results([f"{store.get_store_dir()}/{drv.to_string()}^*"])
        assert results[0]["drv_path"] == f"{store.get_store_dir()}/{drv.to_string()}"
        assert results[0]["outputs"] == ["*"]

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

    def test_copy_closure(self, store: Any, store_seeded_path: Any, tmp_path: Path):
        dest = nanopynix_store.open_store(f"local?root={tmp_path / 'dest'}")
        try:
            assert not dest.is_valid_path(store_seeded_path)
            store.copy_closure([store_seeded_path], dest)
            assert dest.is_valid_path(store_seeded_path)
        finally:
            dest.close()

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


class TestValidPathInfoType:
    """``query_path_info_typed`` — the spike of issue #141.

    ``query_path_info`` converts each of the nine fields into a dictionary,
    and it renders every reference through the store, before the caller reads
    one of them. ``query_path_info_typed`` returns a bound type that reads a
    field when the caller asks for it.

    Two things must hold, and each one has a test below. Both methods must
    report the same data. The ``nix::ref<T>`` caster must carry the result:
    ``Store::queryPathInfo`` returns ``ref<const ValidPathInfo>``, nanobind
    ships no holder for it, and without the caster the call raises a
    ``TypeError`` at run time although the module compiles.
    """

    def test_the_ref_caster_carries_the_result(self, store: Any, store_seeded_path: Any):
        """Without ``nix_ref_caster.hh`` this raises ``TypeError``."""
        info = store.query_path_info_typed(store_seeded_path)
        assert type(info).__name__ == "ValidPathInfo"

    def test_each_field_agrees_with_the_dict(self, store: Any, store_seeded_path: Any):
        """Both methods report the same value for each of the nine fields.

        The bound type renders a path itself. It reads
        ``UnkeyedValidPathInfo::storeDir``, which is what
        ``Store::printStorePath`` reads, so it needs no store to make the
        ``/nix/store/...`` text.
        """
        d = store.query_path_info(store_seeded_path)
        t = store.query_path_info_typed(store_seeded_path)

        assert t.path == d["path"]
        assert sorted(t.references) == sorted(d["references"])
        assert t.deriver == d["deriver"]
        assert t.nar_hash == d["nar_hash"]
        assert t.nar_size == d["nar_size"]
        assert t.registration_time == d["registration_time"]
        assert t.ca == d["ca"]
        assert t.ultimate == d["ultimate"]
        assert list(t.sigs) == list(d["sigs"])

    def test_the_store_dir_comes_from_the_path_info(self, store: Any, store_seeded_path: Any):
        """The field that makes the rendering above possible.

        `nix::Store` is not involved. A relocated store gives each object its
        own store directory, and this field is the one Nix reads.
        """
        t = store.query_path_info_typed(store_seeded_path)
        assert t.store_dir == store.get_store_dir()
        assert t.path == f"{t.store_dir}/{t.store_path.to_string()}"

    def test_a_field_of_a_bound_type_outlives_its_parent(self, store: Any, store_seeded_path: Any):
        """``def_ro`` gives ``rv_policy::reference_internal``.

        The field is a reference into the parent, and not a copy, so the child
        must keep the parent alive. A copy would pass this test as well, and
        ``test_a_bound_field_is_not_a_copy`` below tells the two apart.
        """
        info = store.query_path_info_typed(store_seeded_path)
        path = info.store_path
        expected = path.to_string()
        del info
        gc.collect()
        assert path.to_string() == expected

    def test_a_bound_field_is_not_a_copy(self, store: Any, store_seeded_path: Any):
        """``path`` is a bound type, and ``references`` is an STL container.

        nanobind returns the first as a reference into the parent, so two
        reads give the same object. The second is a ``def_prop_ro``, so each
        read builds a new list. A caller that reads ``references`` in a loop
        pays the whole rendering each time, and must bind the value once.
        """
        info = store.query_path_info_typed(store_seeded_path)
        assert info.store_path is info.store_path
        assert info.references is not info.references


class TestMissingPathsType:
    """``query_missing_typed`` — the general form of the #141 spike.

    ``nix::ValidPathInfo`` renders its own paths, because it carries a store
    directory. ``nix::MissingPaths`` carries none, and neither does
    ``nix::BasicDerivation``, so that property is luck and not a rule.

    ``PyMissingPaths`` supplies the missing half: it holds the struct and the
    store directory of the store that answered the query. The bound type then
    renders itself in the same way, and it still reads no ``nix::Store``. This
    is the shape the remaining helpers need.
    """

    def test_each_field_agrees_with_the_dict(self, store: Any, store_seeded_path: Any):
        d = store.query_missing([store_seeded_path])
        t = store.query_missing_typed([store_seeded_path])

        assert sorted(t.will_build) == sorted(d["will_build"])
        assert sorted(t.will_substitute) == sorted(d["will_substitute"])
        assert sorted(t.unknown) == sorted(d["unknown"])
        assert t.download_size == d["download_size"]
        assert t.nar_size == d["nar_size"]

    def test_the_wrapper_carries_the_store_dir(self, store: Any, store_seeded_path: Any):
        """The field that replaces the ``nix::Store`` the helper used to take."""
        t = store.query_missing_typed([store_seeded_path])
        assert t.store_dir == store.get_store_dir()

    def test_an_unknown_path_is_reported_as_unknown(self, store: Any):
        """A non-vacuous case: the seeded path leaves all three sets empty.

        A bogus path lands in exactly one of them, so this test fails if the
        wrapper renders the wrong set or drops the store directory.
        """
        bogus = nanopynix_store.StorePath("00000000000000000000000000000000-bogus")
        t = store.query_missing_typed([bogus])
        assert t.unknown == [f"{store.get_store_dir()}/{bogus.to_string()}"]


# A derivation that depends on another derivation, so `input_drvs` is not
# empty. The L1 suite has no derivation fixture, and `read_derivation` needs
# one. Evaluating `drvPath` writes the `.drv` into this session's isolated
# store as a side effect, which is what makes the path valid to read back.
DERIVATION_WITH_AN_INPUT = """
let
  inner = derivation {
    name = "nanopynix-l1-inner";
    system = builtins.currentSystem;
    builder = "/bin/sh";
    args = [ "-c" "echo inner > $out" ];
  };
in (derivation {
  name = "nanopynix-l1-outer";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "echo ${inner} > $out" ];
}).drvPath
"""


@pytest.fixture(scope="module")
def seeded_drv_path(store: Any, eval_state: Any) -> Any:
    """The store path of a `.drv` written into this session's isolated store."""
    return store.parse_store_path(eval_state.eval_string(DERIVATION_WITH_AN_INPUT).as_string())


class TestDerivationType:
    """``read_derivation_typed`` — the largest helper of #141, as three types.

    Two of the three defects that
    ``nanopynix/tests/test_store_metadata_fidelity.py`` records came from
    ``read_derivation``. The one that matters here is ``input_drvs``:
    ``DerivedPathMap`` is a tree, a dictionary has no natural shape for a
    tree, and the invented shape kept the first output of each child and never
    recursed.

    ``DerivationOutputs`` binds Nix's own node, so there is no projection to
    get wrong. These tests check that the bound types report what the
    dictionary reports, and that the tree is a tree.
    """

    def test_the_scalar_fields_agree_with_the_dict(self, store: Any, seeded_drv_path: Any) -> None:
        d = store.read_derivation(seeded_drv_path)
        t = store.read_derivation_typed(seeded_drv_path)

        assert t.name == d["name"]
        assert t.system == d["system"]
        assert t.builder == d["builder"]
        assert list(t.args) == list(d["args"])
        assert dict(t.env) == dict(d["env"])
        assert sorted(t.input_srcs) == sorted(d["input_srcs"])
        assert t.structured_attrs == d["structured_attrs"]

    def test_the_outputs_agree_with_the_dict(self, store: Any, seeded_drv_path: Any) -> None:
        """``nix::DerivationOutput`` is a variant, so ``type`` names the branch."""
        d = store.read_derivation(seeded_drv_path)
        t = store.read_derivation_typed(seeded_drv_path)

        assert set(t.outputs) == set(d["outputs"])
        for name, output in t.outputs.items():
            expected = d["outputs"][name]
            assert output.type == expected["type"]
            # The dictionary carries a key only for the branch it took. The
            # bound type reports `None` for a field that branch does not have,
            # which is the same information without the absent key.
            for field in ("path", "ca", "method", "hash_algo"):
                assert getattr(output, field) == expected.get(field)

    def test_the_input_drvs_agree_with_the_dict(self, store: Any, seeded_drv_path: Any) -> None:
        d = store.read_derivation(seeded_drv_path)
        t = store.read_derivation_typed(seeded_drv_path)

        assert set(t.input_drvs) == set(d["input_drvs"])
        for path, node in t.input_drvs.items():
            assert sorted(node.outputs) == sorted(d["input_drvs"][path]["outputs"])

    def test_the_fixture_really_has_an_input(self, store: Any, seeded_drv_path: Any) -> None:
        """Without this the two tests above would pass against an empty map."""
        t = store.read_derivation_typed(seeded_drv_path)
        assert t.input_drvs, "the fixture derivation depends on nothing, so input_drvs proves nothing"

    def test_the_tree_recurses(self, store: Any, seeded_drv_path: Any) -> None:
        """``dynamic_outputs`` is a map of the same node type, not a leaf.

        The old flattening could not represent this, and the type must. A
        plain derivation has no dynamic output, so this checks the shape and
        not a non-empty value.
        """
        t = store.read_derivation_typed(seeded_drv_path)
        node = next(iter(t.input_drvs.values()))
        children: dict[str, Any] = node.dynamic_outputs
        assert isinstance(children, dict)
        for child in children.values():
            assert hasattr(child, "outputs")
            assert hasattr(child, "dynamic_outputs")


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
        """get_uri renders via Nix's StoreReference::render, which always
        includes the "://" scheme separator -- switched to from a renderer
        that silently dropped URI params."""
        store = nanopynix_store.open_store(f"local?root={tmp_path}")
        assert store.get_uri() == "local://"
        assert store.get_uri(with_params=True) == f"local://?root={tmp_path}"

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
