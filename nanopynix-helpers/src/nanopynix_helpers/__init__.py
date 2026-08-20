# pyright: reportUnusedImport=false
# Justifies the pragma above. The block below is the type checker's copy of
# this package's public surface: every import in it is a deliberate re-export
# that no runtime line reads, so 'unused' is what a correct entry looks like.
"""Reusable building blocks layered on nanopynix, kept out of its core to avoid pulling in dependencies (e.g. tree-sitter) that most nanopynix consumers don't need."""

from __future__ import annotations

import importlib
import typing

if typing.TYPE_CHECKING:
    from nanopynix_helpers.attr_completion import (
        complete_file_attr_path as complete_file_attr_path,
        complete_flake_fragment as complete_flake_fragment,
    )
    from nanopynix_helpers.build import (
        FodBuildError as FodBuildError,
        build_with_fod_update as build_with_fod_update,
    )
    from nanopynix_helpers.eval_target import (
        AttrPathNotFoundError as AttrPathNotFoundError,
        AttrPathSearch as AttrPathSearch,
        EvaluationTargetError as EvaluationTargetError,
        parse_attr_path as parse_attr_path,
        select_attr as select_attr,
        select_attr_path as select_attr_path,
        select_flake_attr as select_flake_attr,
        show_attr_paths as show_attr_paths,
    )
    from nanopynix_helpers.fod import (
        FodHashLiteral as FodHashLiteral,
        FodHashMismatch as FodHashMismatch,
        FodSourceUpdateError as FodSourceUpdateError,
        derivation_name_from_path as derivation_name_from_path,
        evaluated_derivation_path as evaluated_derivation_path,
        extract_fod_hash_mismatch as extract_fod_hash_mismatch,
        extract_unique_fod_hash_mismatch as extract_unique_fod_hash_mismatch,
        find_fod_hash_literal as find_fod_hash_literal,
        fixed_output_derivations_in_closure as fixed_output_derivations_in_closure,
        is_fixed_output_derivation_in_closure as is_fixed_output_derivation_in_closure,
        mismatch_is_target_fod as mismatch_is_target_fod,
        replace_fod_hash as replace_fod_hash,
    )


#: The module that defines each public name, for :func:`__getattr__`.
#:
#: **The docstring above was not true, and this table makes it true.** The
#: package re-exported every name eagerly, so `from nanopynix_helpers import
#: select_attr` imported `fod`, which imports `tree_sitter_nix`, which imports
#: `tree_sitter_config`, which imports `email_validator` for one `EmailStr`
#: field. Issue #123 measured the chain at 112.2 ms in `pynix`, which reads
#: four names and none of them from `fod`.
#:
#: A module `__getattr__` (PEP 562) is the shape this repository permits; see
#: the same table in `nanopynix/__init__.py`.
_NAME_TO_MODULE: typing.Final[dict[str, str]] = {
    "AttrPathNotFoundError": "nanopynix_helpers.eval_target",
    "AttrPathSearch": "nanopynix_helpers.eval_target",
    "EvaluationTargetError": "nanopynix_helpers.eval_target",
    "FodBuildError": "nanopynix_helpers.build",
    "FodHashLiteral": "nanopynix_helpers.fod",
    "FodHashMismatch": "nanopynix_helpers.fod",
    "FodSourceUpdateError": "nanopynix_helpers.fod",
    "build_with_fod_update": "nanopynix_helpers.build",
    "complete_file_attr_path": "nanopynix_helpers.attr_completion",
    "complete_flake_fragment": "nanopynix_helpers.attr_completion",
    "derivation_name_from_path": "nanopynix_helpers.fod",
    "evaluated_derivation_path": "nanopynix_helpers.fod",
    "extract_fod_hash_mismatch": "nanopynix_helpers.fod",
    "extract_unique_fod_hash_mismatch": "nanopynix_helpers.fod",
    "find_fod_hash_literal": "nanopynix_helpers.fod",
    "fixed_output_derivations_in_closure": "nanopynix_helpers.fod",
    "is_fixed_output_derivation_in_closure": "nanopynix_helpers.fod",
    "mismatch_is_target_fod": "nanopynix_helpers.fod",
    "parse_attr_path": "nanopynix_helpers.eval_target",
    "replace_fod_hash": "nanopynix_helpers.fod",
    "select_attr": "nanopynix_helpers.eval_target",
    "select_attr_path": "nanopynix_helpers.eval_target",
    "select_flake_attr": "nanopynix_helpers.eval_target",
    "show_attr_paths": "nanopynix_helpers.eval_target",
}


def __getattr__(name: str) -> typing.Any:
    """Resolve a public name, and cache it in the module namespace."""
    origin = _NAME_TO_MODULE.get(name)
    if origin is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(origin), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Report the public surface, which ``vars()`` no longer holds."""
    return sorted(set(_NAME_TO_MODULE) | set(globals()))
