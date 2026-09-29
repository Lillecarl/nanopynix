"""The process-wide primop registry, and PrimOpSpec registration against it.

Both the RPC worker (inside its subprocess) and inproc (in the caller's
process) register here. Only the rpc=True case, a manager-side callable that
crosses the worker's process boundary, is worker-specific, and
``rpc/worker/_worker.py`` bridges it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from nanopynix._typechecking import BEARTYPING
from nanopynix.models import PrimOpSpec

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Mapping, Sequence


# Every evaluator opened after a registration gets it, as Nix's global
# `RegisterPrimOp` does.
_registered: dict[str, tuple[int, Callable[..., Any]]] = {}


def register_primop(name: str, arity: int, callback: Callable[..., Any]) -> None:
    """Give every evaluator opened after this call ``builtins.<name>``.

    A second registration of *name* replaces the first.
    """
    _registered[name] = (arity, callback)


def registered_primops() -> dict[str, tuple[int, Callable[..., Any]]]:
    return dict(_registered)


def clear_primops() -> None:
    _registered.clear()


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
        register_primop(spec.name, spec.arity, import_primop_callable(spec.import_path))
