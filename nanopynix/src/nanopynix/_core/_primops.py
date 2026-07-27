"""Shared plumbing for registering PrimOpSpecs against the process-global
nanopynix_expr.register_primop() -- used by both the RPC worker
(rpc/worker/_worker.py, inside its subprocess) and inproc (inproc/_impl.py,
in the caller's own process). Only the rpc=True bridged-callback case (an
arbitrary manager-side Python callable crossing the RPC worker's process
boundary) is worker-specific and stays in _worker.py -- there is no process
boundary to bridge for inproc callers.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from nanopynix_bindings import expr as nanopynix_expr

from nanopynix.models import PrimOpSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


def to_primop_specs(specs: Sequence[PrimOpSpec | Mapping[str, Any]] | None) -> list[PrimOpSpec]:
    """Normalize a sequence of PrimOpSpecs or dicts (e.g. from
    nanopynix.primops' bundled ipaddress_primops()/jsonschema_primops()/
    yaml_primops()) to a list of PrimOpSpecs."""
    return [spec if isinstance(spec, PrimOpSpec) else PrimOpSpec(**spec) for spec in specs or []]


def import_primop_callable(import_path: str) -> Callable[..., Any]:
    module_name, sep, attr_path = import_path.partition(":")
    if not sep or not module_name or not attr_path:
        raise ValueError(f"invalid primop import path: {import_path!r}")
    value: Any = importlib.import_module(module_name)
    for attr in attr_path.split("."):
        value = getattr(value, attr)
    if not callable(value):
        raise TypeError(f"primop import path is not callable: {import_path!r}")
    return value


def register_import_path_primops(specs: Sequence[PrimOpSpec]) -> None:
    """Register every spec via its `import_path` -- the rpc=False case,
    which covers every spec nanopynix.primops bundles. Raises if a spec
    declares rpc=True (a manager-side callable that needs the RPC worker's
    backchannel bridge, see _worker.py's _register_primops) since there is
    no such bridge to call through here.
    """
    for spec in specs:
        if spec.rpc:
            raise ValueError(
                f"primop {spec.name!r} needs rpc=True bridging, not supported by register_import_path_primops"
            )
        nanopynix_expr.register_primop(
            spec.name,
            spec.arity,
            spec.args,
            spec.doc,
            import_primop_callable(spec.import_path),
        )
