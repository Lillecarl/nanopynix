"""Convert the attribute maps of huggorm's objects to proto values."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix_proto.nix import common as common_pb

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Mapping

    from nanopynix._engine import FlakeRef


def _attrs_value(v: str | int | bool) -> common_pb.AttrsValue:
    """Convert one attribute to an AttrsValue proto."""
    if isinstance(v, bool):
        return common_pb.AttrsValue(bool_value=v)
    if isinstance(v, int):
        return common_pb.AttrsValue(int_value=v)
    return common_pb.AttrsValue(string_value=v)


def attrs_value_map(d: Mapping[str, str | int | bool], /) -> dict[str, common_pb.AttrsValue]:
    """Convert an attribute map to the map that a proto field holds."""
    return {k: _attrs_value(v) for k, v in d.items()}


def flake_ref_attrs(ref: FlakeRef, /) -> dict[str, common_pb.AttrsValue]:
    return attrs_value_map(ref.to_attrs())
