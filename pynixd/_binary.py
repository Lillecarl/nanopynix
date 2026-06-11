"""Declarative binary serialization for Nix daemon protocol types.

Pydantic models inherit from ``WireMessage`` to auto-generate
serialize()/deserialize() from type annotations.

Class variables (ClassVar[int]) are skipped — they're protocol constants
like op codes that are written/read by the operation layer, not the message body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, get_type_hints

from pydantic import BaseModel

if TYPE_CHECKING:
    from .types.context import ReadContext, WriteContext

# ── Type registry ──

_READERS: dict[type, Any] = {}
_WRITERS: dict[type, Any] = {}


def register_type(py_type: type, reader, writer) -> None:
    _READERS[py_type] = reader
    _WRITERS[py_type] = writer


# ── Primitive handlers ──

register_type(
    int,
    reader=lambda r: r.read_uint64(),
    writer=lambda v, w: w.write_uint64(v),
)
register_type(
    bool,
    reader=lambda r: bool(r.read_uint64()),
    writer=lambda v, w: w.write_uint64(1 if v else 0),
)
register_type(
    str,
    reader=lambda r: r.read_string(str),
    writer=lambda v, w: w.write_string(v),
)
register_type(
    bytes,
    reader=lambda r: r.read_bytes(),
    writer=lambda v, w: w.write_bytes(v),
)

# set[str] — wire format is count + N strings
register_type(
    set,
    reader=lambda r: r.read_string_set(str),
    writer=lambda v, w: w.write_string_set(v),
)


# ── Field collection ──


def _wire_fields(cls: type[BaseModel]) -> list[tuple[str, type]]:
    """Return (name, type) pairs in declaration order, skipping ClassVars.

    For generic types (e.g. ``set[str]``) the resolved annotation type is
    returned (e.g. ``set``) rather than the parameterized form, so the
    registry lookup can match on the bare type.
    """
    hints = get_type_hints(cls, include_extras=True)
    result = []
    for name in cls.model_fields:
        ann = hints.get(name)
        if ann is None:
            continue
        origin = getattr(ann, "__origin__", None)
        if origin is ClassVar:
            continue
        # For generic types, use the origin (e.g. ``set[str]`` → ``set``)
        resolved = origin if origin is not None else ann
        result.append((name, resolved))
    return result


# ── Base class ──


class WireMessage(BaseModel):
    """Pydantic base class with auto-generated Nix daemon protocol serde.

    Usage:
        class FooResponse(WireMessage):
            valid: int   # uint64 on the wire
            path: str    # length-prefixed UTF-8
    """

    async def serialize(self, ctx: WriteContext) -> None:
        """Write all non-ClassVar fields in declaration order."""
        for name, ann in _wire_fields(type(self)):
            writer = _WRITERS.get(ann)
            if writer is None:
                raise TypeError(f"No writer registered for {ann} in {type(self).__name__}.{name}")
            writer(getattr(self, name), ctx.writer)

    @classmethod
    async def deserialize(cls, ctx: ReadContext):
        """Read all non-ClassVar fields in declaration order."""
        kwargs = {}
        for name, ann in _wire_fields(cls):
            reader = _READERS.get(ann)
            if reader is None:
                raise TypeError(f"No reader registered for {ann} in {cls.__name__}.{name}")
            kwargs[name] = await reader(ctx.reader)
        return cls(**kwargs)
