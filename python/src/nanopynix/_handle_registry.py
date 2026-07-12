"""Worker-side handle registry — allocates int64 handles for session-scoped resources.

Each resource is stored with a type tag so that a single registry can hold
multiple resource kinds.  The handle namespace is shared across all types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HandleRegistry:
    _resources: dict[int, tuple[str, Any]] = field(default_factory=dict)
    _next: int = 1

    def allocate(self, resource: Any, kind: str) -> int:
        handle = self._next
        self._next += 1
        self._resources[handle] = (kind, resource)
        return handle

    def get(self, handle: int) -> tuple[str, Any]:
        entry = self._resources.get(handle)
        if entry is None:
            raise KeyError(f"handle {handle} not found")
        return entry

    def get_typed(self, handle: int, expected_kind: str) -> Any:
        kind, resource = self.get(handle)
        if kind != expected_kind:
            raise TypeError(
                f"handle {handle} is a {kind}, not a {expected_kind}"
            )
        return resource

    def release(self, handle: int) -> None:
        self._resources.pop(handle, None)

    def clear(self) -> None:
        self._resources.clear()
        self._next = 1
