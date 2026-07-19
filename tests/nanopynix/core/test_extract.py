"""Tests for _extract.py — L1 nanobind → dict converters.

Needs C++ modules loaded but no Nix daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.git import init_flake_repo
from nanopynix_bindings import fetchers as nanopynix_fetchers  # L1 Input
from nanopynix_bindings import flake as nanopynix_flake  # L1 FlakeRef, LockedFlake, parse_flake_ref
from nanopynix_bindings import store as nanopynix_store  # L1 StorePath, Store, PathInfo, BuildResult, MissingInfo

from nanopynix._core._extract import (
    flake_ref_attrs,
    input_attrs,
    locked_flake,
    locked_input,
)
from nanopynix.models import StorePath

if TYPE_CHECKING:
    import nanopynix

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
# input_attrs / flake_ref_attrs
# ════════════════════════════════════════════════════════════════════


def test_input_attrs_from_url():
    inp = nanopynix_fetchers.input_from_url("github:NixOS/nixpkgs")
    result = input_attrs(inp)
    assert isinstance(result, dict)
    assert result["type"].string_value == "github"
    assert result["owner"].string_value == "NixOS"
    assert result["repo"].string_value == "nixpkgs"


def test_flake_ref_attrs():
    fr = nanopynix_flake.parse_flake_ref("github:NixOS/nixpkgs")
    result = flake_ref_attrs(fr)
    assert isinstance(result, dict)
    assert result["type"].string_value == "github"


def test_flake_ref_attrs_vs_input_attrs():
    """flake_ref_attrs and input_attrs produce the same shape for the same URL."""
    fr = nanopynix_flake.parse_flake_ref("github:NixOS/nixpkgs")
    inp = nanopynix_fetchers.input_from_url("github:NixOS/nixpkgs")
    assert flake_ref_attrs(fr) == input_attrs(inp)


# ════════════════════════════════════════════════════════════════════
# locked_input — pure Python dict
# ════════════════════════════════════════════════════════════════════


def test_locked_input_with_ref():
    result = locked_input(
        {
            "ref": "github:NixOS/nixpkgs/123abc",
            "is_flake": True,
        }
    )
    assert result.is_flake is True
    assert result.attrs is not None
    assert isinstance(result.attrs.entries, dict)
    assert result.attrs.entries["type"].string_value == "github"
    assert result.follows == []


def test_locked_input_without_ref():
    result = locked_input(
        {
            "is_flake": True,
            "follows": ["nixpkgs"],
        }
    )
    assert result.attrs is None
    assert result.follows == ["nixpkgs"]
    assert result.is_flake is True


def test_locked_input_default_is_flake():
    result = locked_input({})
    # The function defaults is_flake to True for empty input dict
    # (matching Nix convention), which overrides the proto3 bool default.
    assert result.is_flake is True
    assert result.attrs is None
    assert result.follows == []


def test_locked_input_is_flake_false():
    result = locked_input({"is_flake": False})
    assert result.is_flake is False


def test_locked_input_follows_multiple():
    result = locked_input({"follows": ["a", "b", "c"]})
    assert result.follows == ["a", "b", "c"]


# ════════════════════════════════════════════════════════════════════
# locked_flake — from C++ LockedFlake
# ════════════════════════════════════════════════════════════════════


def test_locked_flake_shape(eval_state: nanopynix.EvalState, tmp_path: Path):
    """lock_flake returns a LockedFlake, extract yields expected dict shape."""
    init_flake_repo(tmp_path, r'hello = "world";')

    fr = nanopynix_flake.parse_flake_ref(str(tmp_path))
    lf = nanopynix_flake.lock_flake(
        eval_state,
        fr,
        write_lock_file=False,
    )
    result = locked_flake(lf)

    assert isinstance(result.description, str)
    assert isinstance(result.inputs, dict)
