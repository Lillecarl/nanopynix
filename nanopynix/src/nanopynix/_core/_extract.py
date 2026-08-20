"""Convert L1 nanobind objects to proto messages.

After the 2026-07-05 C++ boundary refactor, most L1 types (PathInfo,
BuildResult, MissingInfo) return nb::dict directly. Input, FlakeRef, and
LockedFlake still need explicit extraction.
"""

from __future__ import annotations

from typing import Any

from nanopynix_proto.nix import common as common_pb


def _attrs_value(v: Any) -> common_pb.AttrsValue:
    """Convert a Python bool/int/str value to an AttrsValue proto."""
    if isinstance(v, bool):
        return common_pb.AttrsValue(bool_value=v)
    if isinstance(v, int):
        return common_pb.AttrsValue(int_value=v)
    return common_pb.AttrsValue(string_value=str(v))


def _attrs_map(d: dict[str, Any]) -> common_pb.AttrsMap:  # type: ignore[reportUnusedFunction] -- kept as a util for future callers
    """Convert a ``dict[str, bool|int|str]`` to an AttrsMap proto."""
    return common_pb.AttrsMap(entries={k: _attrs_value(v) for k, v in d.items()})


def attrs_value_map(d: dict[str, Any], /) -> dict[str, common_pb.AttrsValue]:
    """Convert a raw L1 attrs dict to the map that a proto field holds."""
    return {str(k): _attrs_value(v) for k, v in d.items()}


def input_attrs(inp: Any, /) -> dict[str, common_pb.AttrsValue]:
    """Extract L1 Input.to_attrs() to ``dict[str, AttrsValue]``."""
    return attrs_value_map(inp.to_attrs())


def flake_ref_attrs(fr: Any, /) -> dict[str, common_pb.AttrsValue]:
    """Extract L1 FlakeRef.to_attrs() to ``dict[str, AttrsValue]``."""
    return input_attrs(fr)  # to_attrs() has the same shape on both types


def locked_node(node: dict[str, Any], /) -> common_pb.LockedNode:
    """Extract one L1 ``LockedFlake.find_input()`` result to a LockedNode proto."""
    return common_pb.LockedNode(
        locked_ref=str(node["locked_ref"]),
        original_ref=str(node["original_ref"]),
        is_flake=bool(node["is_flake"]),
    )


def locked_flake(lf: Any, /) -> common_pb.LockedFlake:
    """Extract a L1 LockedFlake to a proto LockedFlake message."""
    description_raw = lf.description() if callable(lf.description) else lf.description
    return common_pb.LockedFlake(description=str(description_raw))
