"""Normalize nanobind containers into proto-shaped plain Python data.

Split out of the former ``_service_adapter`` module. That module existed to
reflect generated RPC services onto the bindings' proto-dict entrypoints; the
store handler was its only consumer and now has one typed method per RPC, so
the reflection is gone. This helper is the part that outlived it: the eval
worker still receives ``nb::dict``/``nb::list`` from C++ in the few places
where a build result crosses the boundary as a mapping, and betterproto2's
``from_dict`` needs plain ``dict``/``list`` rather than nanobind's own
sequence types.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast


def proto_shape(value: object) -> Any:
    """Recursively convert nanobind mappings/sequences to plain dict/list."""
    if isinstance(value, Mapping):
        return {str(k): proto_shape(v) for k, v in value.items()}  # type: ignore[reportUnknownArgumentType] -- Mapping narrowed without type params
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [proto_shape(v) for v in cast("Sequence[Any]", value)]
    return value
