# pyright: reportUnusedImport=false
"""Reusable building blocks layered on nanopynix, kept out of its core to avoid pulling in dependencies (e.g. tree-sitter) that most nanopynix consumers don't need."""

from __future__ import annotations

from nanopynix_helpers.build import (
    FodBuildError as FodBuildError,
    build_with_fod_update as build_with_fod_update,
)
from nanopynix_helpers.eval_target import (
    EvaluationTargetError as EvaluationTargetError,
    select_attr as select_attr,
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
