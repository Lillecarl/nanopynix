"""Worker-side handle registry — allocates int64 handles for session-scoped resources.

Each resource is stored with a type tag so that a single registry can hold
multiple resource kinds.  The handle namespace is shared across all types.

Thread-safe: accessed from both the event loop thread and the Nix thread.
Uses ``threading.Lock`` to protect the internal dict.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from nanopynix.models import HandleKind


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

    def get_typed(self, handle: int, expected_kind: HandleKind) -> Any:
        kind, resource = self.get(handle)
        if kind != expected_kind:
            raise TypeError(f"handle {handle} is a {kind}, not a {expected_kind}")
        return resource

    def iter_kind(self, kind: HandleKind) -> list[tuple[int, Any]]:
        with self._lock:
            return [(handle, resource) for handle, (k, resource, _owner) in self._resources.items() if k == kind]

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

    def clear(self) -> None:
        with self._lock:
            self._resources.clear()
            self._next = 1
