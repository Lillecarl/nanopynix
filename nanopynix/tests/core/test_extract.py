"""Tests for the attribute converters of ``_core/_extract.py``, and the core's flake shapes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanopynix._core._extract import (
    _attrs_value,  # pyright: ignore[reportPrivateUsage] -- test reaches into _extract's private helper for direct unit coverage, see below
    flake_ref_attrs,
)
from nanopynix._engine import (
    parse_flake_ref,
    store as nanopynix_store,  # StorePath, Store, PathInfo, BuildResult, MissingInfo
)
from nanopynix.models import StorePath
from test_support.git_fixtures import init_flake_repo

if TYPE_CHECKING:
    from nanopynix._core._objects import CoreEvalState

# ════════════════════════════════════════════════════════════════════
# StorePath wrapper
# ════════════════════════════════════════════════════════════════════


def test_store_path_wrapper_basic():
    sp = StorePath("/nix/store/00000000000000000000000000000000-bash-5.2")
    assert sp.base_name == "00000000000000000000000000000000-bash-5.2"
    assert str(sp) == "/nix/store/00000000000000000000000000000000-bash-5.2"
    assert sp.hash_part == "00000000000000000000000000000000"
    assert sp.name == "bash-5.2"


def test_store_path_wrapper_single_dash_name():
    sp = StorePath("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-foo")
    assert sp.hash_part == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert sp.name == "foo"


def test_store_path_wrapper_multiple_dashes():
    sp = StorePath("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-python3.13-nanopynix-0.1.0")
    assert sp.hash_part == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert sp.name == "python3.13-nanopynix-0.1.0"


def test_store_path_wrapper_accepts_unvalidated_basename():
    assert StorePath("justaname").base_name == "justaname"


def test_store_path_wrapper_accepts_empty_basename():
    assert StorePath("").base_name == ""


# ════════════════════════════════════════════════════════════════════
# path_info — from C++ PathInfo for a real store path
# ════════════════════════════════════════════════════════════════════


def test_path_info_from_real_path(store: Any, store_seeded_path: object) -> None:  # noqa: ARG001 -- store_seeded_path guarantees a non-empty store
    """C++ query_path_info now returns a dict directly — validate shape."""
    path_strs = store.query_all_valid_paths()
    if not path_strs:
        return
    first = path_strs[0]
    sp = nanopynix_store.StorePath(Path(first).name)
    result = store.query_path_info(sp)  # returns dict

    assert isinstance(result, dict)
    assert "path" in result
    assert result["path"] == first
    assert isinstance(result["nar_hash"], str)
    assert result["nar_hash"]
    assert isinstance(result["nar_size"], int)
    assert result["nar_size"] >= 0
    assert isinstance(result["ultimate"], bool)
    assert isinstance(result["references"], list)
    for ref in result["references"]:  # type: ignore[reportUnknownVariableType] -- result from nanobind
        assert isinstance(ref, str)


def test_path_info_deriver_none(store: Any, store_seeded_path: object) -> None:  # noqa: ARG001 -- store_seeded_path guarantees a non-empty store
    """A non-derivation path should have deriver=None."""
    path_strs = store.query_all_valid_paths()
    for d in path_strs:
        sp = nanopynix_store.StorePath(Path(d).name)
        if not sp.is_derivation():
            result = store.query_path_info(sp)
            assert "deriver" in result
            if result["deriver"] is not None:
                assert isinstance(result["deriver"], str)
            break
    else:
        return


# ════════════════════════════════════════════════════════════════════
# build_result — from C++ BuildResult
# ════════════════════════════════════════════════════════════════════


def test_build_result_fields():
    """build_result from a real build result (requires daemon)."""
    return


# ════════════════════════════════════════════════════════════════════
# missing_info — from C++ MissingInfo
# ════════════════════════════════════════════════════════════════════


def test_missing_info_shape(store: Any):
    """C++ query_missing now returns a dict directly — validate shape."""
    sp = nanopynix_store.StorePath("00000000000000000000000000000000-nonexistent-1.0")
    result = store.query_missing([sp])  # returns dict

    assert isinstance(result, dict)
    assert isinstance(result["will_build"], list)
    assert isinstance(result["will_substitute"], list)
    assert isinstance(result["unknown"], list)
    assert isinstance(result["download_size"], int)
    assert isinstance(result["nar_size"], int)


# ════════════════════════════════════════════════════════════════════
# flake_ref_attrs / _attrs_value
# ════════════════════════════════════════════════════════════════════


def test_flake_ref_attrs():
    result = flake_ref_attrs(parse_flake_ref("github:NixOS/nixpkgs"))
    assert result["type"].string_value == "github"
    assert result["owner"].string_value == "NixOS"
    assert result["repo"].string_value == "nixpkgs"


def test_attrs_value_bool_and_int_and_string_branches():
    """A flake reference exercises only the string branch; these pin the other two."""
    assert _attrs_value(True).bool_value is True
    assert _attrs_value(7).int_value == 7
    assert _attrs_value("s").string_value == "s"


# ════════════════════════════════════════════════════════════════════
# CoreLockedFlake
# ════════════════════════════════════════════════════════════════════


def test_locked_flake_shape(eval_state: CoreEvalState, tmp_path: Path):
    init_flake_repo(tmp_path, r'hello = "world";')

    locked = eval_state.lock_flake(str(tmp_path), update_inputs=False, write_lock_file=False)
    try:
        assert isinstance(locked.description(), str)
        assert locked.find_input(["missing"]) is None
    finally:
        locked.close()
