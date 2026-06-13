"""Convert L1 nanobind objects to plain dicts for Pydantic model validation.

Every function accepts a nanobind object (or L1-exposed Python value) and
returns a ``dict`` ready for ``SomeModel.model_validate(...)``.
"""

from __future__ import annotations


def store_path(sp, /) -> dict:
    """Extract a L1 StorePath to a dict."""
    return {
        "to_string": sp.to_string(),
        "hash_part": sp.hash_part(),
        "name": sp.name(),
    }


def store_path_str(s: str, /) -> dict:
    """Convert a raw StorePath string (``<hash>-<name>``) to a dict."""
    hyphen = s.index("-")
    return {
        "to_string": s,
        "hash_part": s[:hyphen],
        "name": s[hyphen + 1:],
    }


def path_info(pi, /) -> dict:
    """Extract a L1 PathInfo to a dict."""
    refs = [store_path_str(s) if isinstance(s, str) else store_path(s)
            for s in pi.references]
    drv = pi.deriver
    return {
        "path": store_path(pi.path),
        "nar_hash": pi.nar_hash,
        "nar_size": int(pi.nar_size),
        "registration_time": int(pi.registration_time) if pi.registration_time is not None else None,
        "deriver": store_path(drv) if drv is not None else None,
        "references": refs,
        "ca": pi.ca if pi.ca else None,
        "ultimate": bool(pi.ultimate),
    }


def build_result(br, /) -> dict:
    """Extract a L1 BuildResult to a dict."""
    return {
        "drv_path": br.drv_path,
        "success": bool(br.success),
        "status": br.status,
        "error_msg": br.error_msg,
    }


def missing_info(mi, /) -> dict:
    """Extract a L1 MissingInfo to a dict."""
    return {
        "will_build": [store_path_str(s) for s in mi.will_build],
        "will_substitute": [store_path_str(s) for s in mi.will_substitute],
        "unknown": [store_path_str(s) for s in mi.unknown],
        "download_size": int(mi.download_size),
        "nar_size": int(mi.nar_size),
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
        import nanopynix_flake
        fr = nanopynix_flake.parse_flake_ref(ref_str)
        result["attrs"] = flake_ref_attrs(fr)
    else:
        result["attrs"] = None

    follows = li_dict.get("follows", [])
    result["follows"] = [str(f) for f in follows]

    return result


def locked_flake(lf, /) -> dict:
    """Extract a L1 LockedFlake to a dict."""
    inputs = {}
    for k in lf.inputs:
        inputs[str(k)] = locked_input(lf.inputs[k])
    return {
        "description": lf.description() if callable(lf.description) else str(lf.description),
        "inputs": inputs,
    }
