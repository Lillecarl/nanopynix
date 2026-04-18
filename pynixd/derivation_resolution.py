"""Derivation resolution for deferred (non-CA) derivations that depend on CA derivations.

When a deferred derivation depends on a CA derivation, the BuildDerivation
wire protocol sends a BasicDerivation (no inputDrvs) which the daemon cannot
resolve. This module implements the Nix `tryResolve` + `rewriteDerivation`
algorithm to resolve deferred derivations before sending them to the daemon.

The resolution flow:
1. Compute DownstreamPlaceholder for each input CA derivation output
2. Build a placeholder -> actual_path rewrite map
3. Apply rewrites to builder, args, and env
4. Move inputDrv outputs into inputSrcs
5. Compute hashDerivationModulo on the resolved derivation (masked)
6. Derive output paths via makeOutputPath
7. Convert Deferred outputs to InputAddressed
"""

from __future__ import annotations

import hashlib

from .drv_parser import ParsedDerivation
from .operations.base import BasicDerivation, DerivationOutput
from .store_path import StorePath

NIX32_CHARS = "0123456789abcdfghijklmnpqrsvwxyz"
STORE_DIR = "/nix/store"


def nix32_encode(data: bytes) -> str:
    if len(data) == 0:
        return ""
    size = len(data)
    result_len = (size * 8 - 1) // 5 + 1
    result: list[str] = []
    for n in range(result_len - 1, -1, -1):
        b = n * 5
        i = b // 8
        j = b % 8
        c = (data[i] >> j) & 0x1F
        if i + 1 < size:
            c |= (data[i + 1] << (8 - j)) & 0x1F
        result.append(NIX32_CHARS[c])
    return "".join(result)


def _output_path_name(drv_name: str, output_name: str) -> str:
    if output_name == "out":
        return drv_name
    return f"{drv_name}-{output_name}"


def _nix_store_path_name(store_path_str: str) -> str:
    basename = store_path_str.rsplit("/", 1)[-1]
    first_dash = basename.find("-")
    if first_dash == -1:
        return basename
    return basename[first_dash + 1 :]


def _nix_drv_name(drv_path: StorePath) -> str:
    name_with_ext = _nix_store_path_name(str(drv_path))
    if name_with_ext.endswith(".drv"):
        return name_with_ext[:-4]
    return name_with_ext


def downstream_placeholder(drv_path: StorePath, output_name: str) -> str:
    hash_part = str(drv_path).rsplit("/", 1)[-1].split("-", 1)[0]
    drv_name = _nix_drv_name(drv_path)
    clear_text = (
        f"nix-upstream-output:{hash_part}:{_output_path_name(drv_name, output_name)}"
    )
    h = hashlib.sha256(clear_text.encode()).digest()
    return "/" + nix32_encode(h)


def _compress_hash(data: bytes, new_size: int) -> bytes:
    result = bytearray(new_size)
    for i in range(len(data)):
        result[i % new_size] ^= data[i]
    return bytes(result)


def _make_store_path(
    type_str: str, hash_modulo: bytes, name: str, store_dir: str = STORE_DIR
) -> str:
    hash_str = "sha256:" + hash_modulo.hex()
    s = f"{type_str}:{hash_str}:{store_dir}:{name}"
    digest = hashlib.sha256(s.encode()).digest()
    compressed = _compress_hash(digest, 20)
    return f"{store_dir}/{nix32_encode(compressed)}-{name}"


def _make_output_path(
    output_id: str, hash_modulo: bytes, drv_name: str, store_dir: str = STORE_DIR
) -> str:
    name = _output_path_name(drv_name, output_id)
    return _make_store_path(f"output:{output_id}", hash_modulo, name, store_dir)


def _unparse_basic_derivation(drv: BasicDerivation, mask_outputs: bool = True) -> str:
    parts: list[str] = ["Derive("]

    out_parts: list[str] = []
    for name, o in sorted(drv.outputs.items()):
        path = "" if mask_outputs else o.path
        out_parts.append(f'("{name}","{path}","{o.method}","{o.hash_digest}")')
    parts.append(f"[{','.join(out_parts)}],")

    parts.append("[],")

    srcs = ",".join(f'"{p}"' for p in sorted(str(p) for p in drv.input_srcs))
    parts.append(f"[{srcs}],")

    parts.append(f'"{drv.platform}",')
    parts.append(f'"{drv.builder}",')

    args = ",".join(f'"{a}"' for a in drv.args)
    parts.append(f"[{args}],")

    env_parts: list[str] = []
    for k, v in sorted(drv.env.items()):
        env_parts.append(f'("{k}","{v}")')
    parts.append(f"[{','.join(env_parts)}]")

    parts.append(")")
    return "".join(parts)


def _hash_derivation_modulo(
    drv: BasicDerivation, mask_outputs: bool = True
) -> dict[str, bytes]:
    aterm = _unparse_basic_derivation(drv, mask_outputs=mask_outputs)
    h = hashlib.sha256(aterm.encode()).digest()
    return {name: h for name in drv.outputs}


def _rewrite_strings(s: str, rewrites: dict[str, str]) -> str:
    for old, new in rewrites.items():
        if old == new:
            continue
        s = s.replace(old, new)
    return s


def resolve_derivation(
    drv: ParsedDerivation,
    drv_path: StorePath,
    resolved_output_paths: dict[str, StorePath],
) -> BasicDerivation:
    """Resolve a deferred derivation by substituting placeholders with actual paths.

    Args:
        drv: The parsed derivation (with inputDrv info)
        drv_path: The .drv store path (for computing placeholders and output names)
        resolved_output_paths: {output_name: actual_store_path} for each
            input derivation's outputs

    Returns:
        A resolved BasicDerivation with filled-in output paths.
        The wire BasicDerivation has no inputDrvs, placeholders rewritten,
        and Deferred outputs converted to InputAddressed.
    """
    drv_name = _nix_drv_name(drv_path)

    rewrites: dict[str, str] = {}
    new_input_srcs: set[StorePath] = set(drv.input_srcs)

    for input_drv_path, output_names in drv.input_drvs.items():
        for output_name in output_names:
            placeholder = downstream_placeholder(input_drv_path, output_name)
            actual_path = resolved_output_paths.get(output_name)
            if actual_path is None:
                raise ValueError(f"No resolved path for {input_drv_path}!{output_name}")
            rewrites[placeholder] = str(actual_path)
            new_input_srcs.add(StorePath(str(actual_path)))

    resolved = BasicDerivation(
        outputs={
            o.name: DerivationOutput(
                path=o.path,
                method=o.hash_algo,
                hash_digest=o.hash_value,
            )
            for o in drv.outputs
        },
        input_srcs=new_input_srcs,
        platform=drv.platform,
        builder=_rewrite_strings(drv.builder, rewrites),
        args=[_rewrite_strings(a, rewrites) for a in drv.args],
        env={k: _rewrite_strings(v, rewrites) for k, v in drv.env.items()},
        is_dynamic=drv.is_dynamic,
    )

    hash_modulo = _hash_derivation_modulo(resolved, mask_outputs=True)

    new_outputs: dict[str, DerivationOutput] = {}
    for name, o in resolved.outputs.items():
        if o.path == "" and o.method == "" and o.hash_digest == "":
            h = hash_modulo[name]
            out_path = _make_output_path(name, h, drv_name)
            new_outputs[name] = DerivationOutput(
                path=out_path, method="", hash_digest=""
            )
            resolved.env[name] = out_path
        else:
            new_outputs[name] = o

    resolved.outputs = new_outputs
    return resolved
