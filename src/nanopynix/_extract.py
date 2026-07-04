"""Convert L1 nanobind objects to plain dicts for Pydantic model validation.

After the 2026-07-05 C++ boundary refactor, most L1 types (PathInfo,
BuildResult, MissingInfo) return nb::dict directly.  Only StorePath and
Input/FlakeRef/LockedFlake still need explicit extraction.
"""

from __future__ import annotations

import nanopynix_flake


def store_path(sp, /) -> dict:
    """Extract a L1 StorePath to a dict."""
    return {
        "to_string": sp.to_string(),
        "hash_part": sp.hash_part(),
        "name": sp.name(),
    }


def store_path_str(s: str, /) -> dict:
    """Parse a raw StorePath string (``<store-dir>/<hash>-<name>``) to a dict.

    The hash part is always the segment before the first ``-`` — Nix hash
    encodings (base32) never contain ``-``, so ``str.index`` is reliable here.
    """
    try:
        hyphen = s.index("-")
    except ValueError:
        raise ValueError(f"Invalid store path: no '-' separator in '{s}'") from None
    return {
        "to_string": s,
        "hash_part": s[:hyphen],
        "name": s[hyphen + 1:],
    }


def input_attrs(inp, /) -> dict[str, str | int | bool]:
    """Extract L1 Input.to_attrs() to plain Python dict."""
    raw = inp.to_attrs()
    return {str(k): v for k, v in raw.items()}


def flake_ref_attrs(fr, /) -> dict[str, str | int | bool]:
    """Extract L1 FlakeRef.to_attrs() to plain Python dict."""
    return input_attrs(fr)  # to_attrs() has the same shape on both types


def locked_input(li_dict: dict, /) -> dict:
    """Extract a dict from LockedFlake.inputs values to LockedInput dict."""
    result: dict = {"is_flake": bool(li_dict.get("is_flake", True))}

    if "ref" in li_dict:
        ref_str = str(li_dict["ref"])
        try:
            fr = nanopynix_flake.parse_flake_ref(ref_str)
        except Exception:
            # Malformed ref — return as raw string attrs
            result["attrs"] = {"ref": ref_str}
        else:
            result["attrs"] = flake_ref_attrs(fr)
    else:
        result["attrs"] = None

    follows = li_dict.get("follows", [])
    result["follows"] = [str(f) for f in follows]

    return result


def locked_flake(lf, /) -> dict:
    """Extract a L1 LockedFlake to a dict."""
    inputs = {}
    lf_inputs = lf.inputs()
    for k in lf_inputs:
        inputs[str(k)] = locked_input(lf_inputs[k])
    return {
        "description": lf.description() if callable(lf.description) else str(lf.description),
        "inputs": inputs,
    }
