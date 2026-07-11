"""Convert L1 nanobind objects to proto messages.

After the 2026-07-05 C++ boundary refactor, most L1 types (PathInfo,
BuildResult, MissingInfo) return nb::dict directly.  Only StorePath and
Input/FlakeRef/LockedFlake still need explicit extraction.
"""

from __future__ import annotations

from typing import Any

from nanopynix_proto.nix import common as common_pb

import nanopynix_flake


def _attrs_value(v: Any) -> common_pb.AttrsValue:
    """Convert a Python bool/int/str value to an AttrsValue proto."""
    if isinstance(v, bool):
        return common_pb.AttrsValue(bool_value=v)
    if isinstance(v, int):
        return common_pb.AttrsValue(int_value=v)
    return common_pb.AttrsValue(string_value=str(v))


def store_path(sp, /) -> common_pb.StorePath:
    """Extract a L1 StorePath to a proto StorePath message."""
    return common_pb.StorePath(
        to_string=sp.to_string(),
        hash_part=sp.hash_part(),
        name=sp.name(),
    )


def store_path_str(s: str, /) -> common_pb.StorePath:
    """Parse a raw StorePath string (``<hash>-<name>``) to a proto StorePath.

    The hash part is always the segment before the first ``-`` — Nix hash
    encodings (base32) never contain ``-``, so ``str.index`` is reliable here.
    """
    try:
        hyphen = s.index("-")
    except ValueError:
        raise ValueError(f"Invalid store path: no '-' separator in '{s}'") from None
    return common_pb.StorePath(
        to_string=s,
        hash_part=s[:hyphen],
        name=s[hyphen + 1 :],
    )


def _attrs_map(d: dict[str, Any]) -> common_pb.AttrsMap:
    """Convert a ``dict[str, bool|int|str]`` to an AttrsMap proto."""
    return common_pb.AttrsMap(entries={k: _attrs_value(v) for k, v in d.items()})


def input_attrs(inp, /) -> dict[str, common_pb.AttrsValue]:
    """Extract L1 Input.to_attrs() to ``dict[str, AttrsValue]``."""
    raw = inp.to_attrs()
    return {str(k): _attrs_value(v) for k, v in raw.items()}


def flake_ref_attrs(fr, /) -> dict[str, common_pb.AttrsValue]:
    """Extract L1 FlakeRef.to_attrs() to ``dict[str, AttrsValue]``."""
    return input_attrs(fr)  # to_attrs() has the same shape on both types


def locked_input(li_dict: dict, /) -> common_pb.LockedInput:
    """Extract a dict from LockedFlake.inputs values to LockedInput proto."""
    is_flake: bool = bool(li_dict.get("is_flake", True))

    attrs: common_pb.AttrsMap | None = None
    if "ref" in li_dict:
        ref_str = str(li_dict["ref"])
        try:
            fr = nanopynix_flake.parse_flake_ref(ref_str)
        except Exception:
            # Malformed ref — return as raw string attrs
            attrs = common_pb.AttrsMap(entries={"ref": _attrs_value(ref_str)})
        else:
            # flake_ref_attrs already returns dict[str, AttrsValue];
            # pass it directly — _attrs_map would double-wrap.
            attrs = common_pb.AttrsMap(entries=flake_ref_attrs(fr))

    follows_raw = li_dict.get("follows", [])
    follows = [str(f) for f in follows_raw]

    return common_pb.LockedInput(attrs=attrs, is_flake=is_flake, follows=follows)


def locked_flake(lf, /) -> common_pb.LockedFlake:
    """Extract a L1 LockedFlake to a proto LockedFlake message."""
    inputs: dict[str, common_pb.LockedInput] = {}
    lf_inputs = lf.inputs()
    for k in lf_inputs:
        inputs[str(k)] = locked_input(lf_inputs[k])
    description_raw = lf.description() if callable(lf.description) else lf.description
    description = str(description_raw)
    return common_pb.LockedFlake(description=description, inputs=inputs)
