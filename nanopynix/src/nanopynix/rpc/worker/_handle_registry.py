"""Worker-side handle registry — allocates int64 handles for session-scoped resources.

Each resource is stored with a type tag so that a single registry can hold
multiple resource kinds.  The handle namespace is shared across all types.

There is one accessor for each kind, rather than one generic accessor that
takes the kind and returns ``Any``. Four kinds exist, they do not change, and
a named accessor is what gives each handler body a type to check. The kind tag
is still verified at run time, so a handle of the wrong kind raises
``TypeError`` as it did before.

``EvalEntry`` lives here, beside the accessor that returns it. It is the one
resource kind that the worker defines itself, and the other three are ``_core``
types. The registry cannot name it from another module without an import
cycle, because ``WorkerState`` constructs a registry.

Thread-safe: accessed from both the event loop thread and the Nix thread.
Uses ``threading.Lock`` to protect the internal dict.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nanopynix._typechecking import BEARTYPING
from nanopynix._wire import HandleKind

if TYPE_CHECKING or BEARTYPING:
    from nanopynix._core._nix_executor import NixThreadExecutor
    from nanopynix._core._objects import CoreEvalState, CoreLockedFlake, CoreStore, CoreValue


@dataclass
class EvalEntry:
    """One worker-hosted evaluator: its state, dedicated Nix thread, and owning store."""

    eval_state: CoreEvalState
    executor: NixThreadExecutor
    store_handle: int


@dataclass
class HandleRegistry:
    _resources: dict[int, tuple[HandleKind, Any, int | None]] = field(
        default_factory=dict[int, tuple[HandleKind, Any, "int | None"]],
    )
    _next: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allocate(self, resource: Any, kind: HandleKind, owner: int | None = None) -> int:
        with self._lock:
            handle = self._next
            self._next += 1
            self._resources[handle] = (kind, resource, owner)
            return handle

    def get(self, handle: int) -> tuple[HandleKind, Any]:
        with self._lock:
            entry = self._resources.get(handle)
            if entry is None:
                raise KeyError(f"handle {handle} not found")
            kind, resource, _owner = entry
            return kind, resource

    def _of_kind(self, handle: int, expected_kind: HandleKind) -> Any:
        """Return the resource once its tag says it is the kind asked for.

        Private, because a caller must go through the accessor that names the
        type. This holds the check that all four accessors share.
        """
        kind, resource = self.get(handle)
        if kind != expected_kind:
            raise TypeError(f"handle {handle} is a {kind}, not a {expected_kind}")
        return resource

    def get_store(self, handle: int) -> CoreStore:
        return self._of_kind(handle, HandleKind.STORE)

    def get_eval_entry(self, handle: int) -> EvalEntry:
        return self._of_kind(handle, HandleKind.EVAL)

    def get_value(self, handle: int) -> CoreValue:
        return self._of_kind(handle, HandleKind.VALUE)

    def get_locked_flake(self, handle: int) -> CoreLockedFlake:
        return self._of_kind(handle, HandleKind.LOCKED_FLAKE)

    def iter_kind(self, kind: HandleKind) -> list[tuple[int, Any]]:
        with self._lock:
            return [(handle, resource) for handle, (k, resource, _owner) in self._resources.items() if k == kind]

    def iter_evals(self) -> list[tuple[int, EvalEntry]]:
        """Every open evaluator, typed. The one caller that reads what ``iter_kind`` returns."""
        return list(self.iter_kind(HandleKind.EVAL))

    def iter_owned(self, owner: int, kind: HandleKind | None = None) -> list[tuple[int, Any]]:
        """Return ``(handle, resource)`` pairs allocated with ``owner``, optionally filtered by ``kind``."""
        with self._lock:
            return [
                (handle, resource)
                for handle, (k, resource, resource_owner) in self._resources.items()
                if resource_owner == owner and (kind is None or k == kind)
            ]

    def release(self, handle: int) -> None:
        with self._lock:
            self._resources.pop(handle, None)
