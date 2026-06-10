"""Shared utility functions for the goal system.

Moved from resolution.py and derivation.py to break circular
dependencies between goal modules.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ..derived_path import DerivedPath
from ..drv_parser import ChildMapNode, _aterm_escape
from ..store_path import StorePath
from ..utils import compress_hash, nix32_encode

# ── DerivedPath helpers ────────────────────────────────────────────


def _fake_dp(drv_path: StorePath, output_name: str) -> DerivedPath:
    """Build a DerivedPath for the goal's path field."""
    from ..derived_path import OutputsNames

    return DerivedPath._from_components(
        drv_path=drv_path,
        chain=(),
        outputs=OutputsNames(frozenset({output_name})),
    )


def _dp_from(drv_path: StorePath, output_name: str) -> DerivedPath:
    """Construct a DerivedPath for (drv_path, output_name)."""
    from ..derived_path import DerivedPath, OutputsNames

    return DerivedPath._from_components(
        drv_path=drv_path,
        chain=(),
        outputs=OutputsNames(frozenset({output_name})),
    )


def _single_output(dp: DerivedPath) -> str:
    """Extract the single output name from a DerivedPath, defaulting to 'out'."""
    from ..derived_path import OutputsAll, OutputsNames

    if dp.is_opaque:
        return ""
    if isinstance(dp.outputs, OutputsAll):
        return "out"
    if isinstance(dp.outputs, OutputsNames):
        names = list(dp.outputs.names)
        return names[0] if names else "out"
    return "out"


# ── Derivation parsing helpers ─────────────────────────────────────


def _find_output(derivation: Any, output_name: str) -> Any | None:
    """Find a DrvOutput by name in the derivation."""

    for o in derivation.outputs:
        if o.name == output_name:
            return o
    return None


def _nix_drv_name(drv_path: StorePath) -> str:
    """Extract the derivation name (without .drv suffix) from a store path."""
    name = str(drv_path).rsplit("/", 1)[-1]
    first_dash = name.find("-")
    if first_dash == -1:
        return name
    name = name[first_dash + 1 :]
    return name.removesuffix(".drv")


def _output_path_name(drv_name: str, output_name: str) -> str:
    """Nix's ``outputPathName`` — the basename part of an output store path."""
    if output_name == "out":
        return drv_name
    return f"{drv_name}-{output_name}"


# ── ChildMapNode helpers ────────────────────────────────────────────


def _child_map_to_paths(drv_path: StorePath, node: ChildMapNode) -> list[DerivedPath]:
    """Walk a ChildMapNode tree and yield a DerivedPath for each leaf."""
    from ..derived_path import OutputsNames

    results: list[DerivedPath] = []

    def _walk(n: ChildMapNode, prefix_chain: tuple[str, ...]) -> None:
        for child_name, child_node in n.children.items():
            _walk(child_node, (*prefix_chain, child_name))
        if n.outputs:
            results.extend(
                DerivedPath._from_components(
                    drv_path=drv_path,
                    chain=prefix_chain,
                    outputs=OutputsNames(frozenset({leaf_out})),
                )
                for leaf_out in n.outputs
            )

    _walk(node, ())
    return results


# ── ATerm serialization helpers ────────────────────────────────────


def _q(s: str) -> str:
    """ATerm-quote a string: wrap in quotes with escaping."""
    return f'"{_aterm_escape(s)}"'


def _q_list(items: list[str]) -> str:
    """Format a list of ATerm-quoted strings: ``["a","b"]``."""
    return "[" + ",".join(_q(o) for o in items) + "]"


def _format_child_map_node(node: ChildMapNode) -> str:
    """Serialize a ChildMapNode to ATerm for hashDerivationModulo."""
    children_parts: list[str] = []
    for child_name, child_node in sorted(node.children.items()):
        inner = _q(child_name) + ",(" + _q_list(child_node.outputs) + ","
        inner += _format_child_map_node(child_node)
        inner += ")"
        children_parts.append(f"({inner})")
    return "[" + ",".join(children_parts) + "]"


# ── hashDerivationModulo ────────────────────────────────────────────


def _unparse_for_hash(
    derivation: Any,
    input_drv_hashes: dict[str, list[str]],
    dynamic_input_drv_hashes: dict[str, ChildMapNode] | None = None,
) -> str:
    """Serialize a Derivation to ATerm for hashDerivationModulo."""
    parts: list[str] = []

    is_dynamic = bool(derivation.dynamic_input_drvs)
    if is_dynamic:
        parts.append("DrvWithVersion(")
        parts.append(_q("xp-dyn-drv"))
        parts.append(",")
    else:
        parts.append("Derive(")

    # Outputs (masked for hashing)
    parts.append("[")
    first = True
    for o in sorted(derivation.outputs, key=lambda x: x.name):
        if first:
            first = False
        else:
            parts.append(",")
        parts.append("(" + _q(o.name) + "," + _q("") + "," + _q(o.hash_algo) + "," + _q(o.hash_value) + ")")
    parts.append("],")

    # Input derivations (replaced with modulo hashes)
    combined: dict[str, tuple[list[str], ChildMapNode | None]] = {}
    for h, outs in input_drv_hashes.items():
        combined[h] = (list(outs), None)
    if dynamic_input_drv_hashes:
        for h, node in dynamic_input_drv_hashes.items():
            existing = combined.get(h)
            if existing is not None:
                flat_outs, existing_node = existing
                if existing_node is not None:
                    existing_node.outputs.extend(node.outputs)
                    existing_node.children.update(node.children)
                else:
                    combined[h] = (flat_outs, node)
            else:
                combined[h] = ([], node)

    parts.append("[")
    first = True
    for h, (flat_outs, dyn_node) in sorted(combined.items(), key=lambda x: x[0]):
        if first:
            first = False
        else:
            parts.append(",")
        quoted_outs = ",".join(_q(o) for o in flat_outs)
        if dyn_node is not None and (dyn_node.outputs or dyn_node.children):
            parts.append(f"({_q(h)},(")
            parts.append(f"[{quoted_outs}]")
            parts.append(",")
            parts.append(_format_child_map_node(dyn_node))
            parts.append("))")
        else:
            parts.append(f"({_q(h)},[{quoted_outs}])")
    parts.append("],")

    # Input sources
    srcs = ",".join(_q(str(p)) for p in sorted(str(p) for p in derivation.input_srcs))
    parts.append(f"[{srcs}],")

    # Platform
    parts.append(_q(derivation.platform) + ",")

    # Builder
    parts.append(_q(derivation.builder) + ",")

    # Arguments
    args = ",".join(_q(a) for a in derivation.args)
    parts.append(f"[{args}],")

    # Environment (output paths masked for hashing)
    output_names = {o.name for o in derivation.outputs}
    parts.append("[")
    first = True
    for k, v in sorted(derivation.env.items()):
        if first:
            first = False
        else:
            parts.append(",")
        val = "" if k in output_names else v
        parts.append(f"({_q(k)},{_q(val)})")
    parts.append("])")

    return "".join(parts)


# ── Store path derivation ───────────────────────────────────────────


def _make_store_path(
    type_str: str,
    hash_modulo: bytes,
    name: str,
    store_dir: str = "/nix/store",
) -> str:
    """Nix's ``makeStorePath`` — derive a store path from a type, hash, and name."""
    hash_hex = hash_modulo.hex()
    s = f"{type_str}:sha256:{hash_hex}:{store_dir}:{name}"
    digest = hashlib.sha256(s.encode()).digest()
    compressed = compress_hash(digest, 20)
    return f"{store_dir}/{nix32_encode(compressed)}-{name}"


def _derive_output_paths(
    derivation: Any,
    modulo_hash_hex: str,
    drv_path: StorePath,
) -> dict[str, StorePath]:
    """Derive output store paths from a modulo hash."""
    drv_name = _nix_drv_name(drv_path)
    h = bytes.fromhex(modulo_hash_hex)
    result: dict[str, StorePath] = {}

    for o in derivation.outputs:
        if o.path:
            result[o.name] = StorePath(o.path)
        else:
            name = _output_path_name(drv_name, o.name)
            out = _make_store_path(f"output:{o.name}", h, name)
            result[o.name] = StorePath(out)

    return result


# ── Goal tree helpers ──────────────────────────────────────────────


if TYPE_CHECKING:
    from ..goals.manager import Goal as GoalType


def _collect_resolved_paths(children: set[GoalType]) -> dict[str, StorePath]:
    """Collect resolved output paths from input deps in the goal tree."""
    result: dict[str, StorePath] = {}

    def _collect(goal: GoalType) -> None:
        if goal.result and goal.result.resolved_outputs:
            result.update(goal.result.resolved_outputs)
        for child in goal.children:
            _collect(child)

    for g in children:
        _collect(g)

    return result
