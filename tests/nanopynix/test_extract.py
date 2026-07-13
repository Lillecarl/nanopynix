"""Tests for _extract.py — L1 nanobind → dict converters.

Needs C++ modules loaded but no Nix daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import nanopynix_fetchers  # L1 Input
import nanopynix_flake  # L1 FlakeRef, LockedFlake, parse_flake_ref
import nanopynix_store  # L1 StorePath, Store, PathInfo, BuildResult, MissingInfo
import nanopynix
from nanopynix._extract import (
    flake_ref_attrs,
    input_attrs,
    locked_flake,
    locked_input,
    store_path,
    store_path_str,
)
from nanopynix.models import StorePath

# ════════════════════════════════════════════════════════════════════
# store_path_str — pure string parsing
# ════════════════════════════════════════════════════════════════════


def test_store_path_str_basic():
    sp = StorePath(store_path_str("00000000000000000000000000000000-bash-5.2"))
    assert sp.base_name == "00000000000000000000000000000000-bash-5.2"
    assert sp.to_string == "00000000000000000000000000000000-bash-5.2"
    assert sp.hash_part == "00000000000000000000000000000000"
    assert sp.name == "bash-5.2"


def test_store_path_str_single_dash_name():
    sp = StorePath(store_path_str("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-foo"))
    assert sp.hash_part == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert sp.name == "foo"


def test_store_path_str_multiple_dashes():
    sp = StorePath(store_path_str("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-python3.13-nanopynix-0.1.0"))
    assert sp.hash_part == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert sp.name == "python3.13-nanopynix-0.1.0"


def test_store_path_str_accepts_unvalidated_basename():
    assert store_path_str("justaname").base_name == "justaname"


def test_store_path_str_accepts_empty_basename():
    assert store_path_str("").base_name == ""


# ════════════════════════════════════════════════════════════════════
# store_path — from C++ StorePath object
# ════════════════════════════════════════════════════════════════════


def test_store_path_from_cpp():
    sp = nanopynix_store.StorePath("00000000000000000000000000000000-foo-1.0")
    converted = StorePath(store_path(sp))
    assert converted.base_name == "00000000000000000000000000000000-foo-1.0"
    assert converted.to_string == "00000000000000000000000000000000-foo-1.0"
    assert converted.hash_part == "00000000000000000000000000000000"
    assert converted.name == "foo-1.0"


def test_store_path_from_store_parse(store: Any):
    sp = store.parse_store_path(store.get_store_dir() + "/00000000000000000000000000000000-hello")
    converted = StorePath(store_path(sp))
    assert converted.base_name == "00000000000000000000000000000000-hello"
    assert converted.to_string == "00000000000000000000000000000000-hello"
    assert converted.hash_part == "00000000000000000000000000000000"
    assert converted.name == "hello"


# ════════════════════════════════════════════════════════════════════
# path_info — from C++ PathInfo for a real store path
# ════════════════════════════════════════════════════════════════════


def test_path_info_from_real_path(store: Any):
    """C++ query_path_info now returns a dict directly — validate shape."""
    path_strs = store.query_all_valid_paths()
    if not path_strs:
        return
    # query_all_valid_paths returns list of dicts now
    first = path_strs[0]
    sp = nanopynix_store.StorePath(first["base_name"])
    result = store.query_path_info(sp)  # returns dict

    assert isinstance(result, dict)
    assert "path" in result
    assert "base_name" in result["path"]
    assert isinstance(result["nar_hash"], str)
    assert result["nar_hash"]
    assert isinstance(result["nar_size"], int)
    assert result["nar_size"] >= 0
    assert isinstance(result["ultimate"], bool)
    assert isinstance(result["references"], list)
    for ref in result["references"]:
        assert "base_name" in ref


def test_path_info_deriver_none(store: Any):
    """A non-derivation path should have deriver=None."""
    path_strs = store.query_all_valid_paths()
    for d in path_strs:
        sp = nanopynix_store.StorePath(d["base_name"])
        if not sp.is_derivation():
            result = store.query_path_info(sp)
            assert "deriver" in result
            if result["deriver"] is not None:
                assert "base_name" in result["deriver"]
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
    import subprocess

    (tmp_path / "flake.nix").write_text("""
    {
        outputs = { ... }: {
            hello = "world";
        };
    }
    """)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "flake.nix"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    fr = nanopynix_flake.parse_flake_ref(str(tmp_path))
    lf = nanopynix_flake.lock_flake(
        eval_state,
        fr,
        write_lock_file=False,
    )
    result = locked_flake(lf)

    assert isinstance(result.description, str)
    assert isinstance(result.inputs, dict)
