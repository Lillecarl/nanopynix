"""Declarative binary serialization for Nix daemon protocol types.

Pydantic models inherit from ``WireMessage`` to auto-generate
serialize()/deserialize() from type annotations.

Class variables (ClassVar[int]) are skipped — they're protocol constants
like op codes that are written/read by the operation layer, not the message body.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, get_type_hints

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .constants import proto
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


# dict[str, str] — wire format is count + N key-value string pairs
async def _read_str_dict(r):
    n = await r.read_uint64()
    result = {}
    for _ in range(n):
        k = await r.read_string(str)
        v = await r.read_string(str)
        result[k] = v
    return result


def _write_str_dict(v, w):
    w.write_uint64(len(v))
    for k, val in v.items():
        w.write_string(k)
        w.write_string(val)


register_type(
    dict,
    reader=_read_str_dict,
    writer=_write_str_dict,
)


# ── Conditional type ──


class Conditional[T]:
    """A protocol field present only when ``valid=true`` on the wire.

    Usage::

        class MyResponse(WireMessage):
            info: Conditional[PathInfo]

    On the wire this reads/writes a ``uint64`` validity flag followed by
    the inner type's fields when present.
    """

    def __init__(self, value: T | None = None):
        self.value = value

    @property
    def is_present(self) -> bool:
        return self.value is not None


# ── Nested model registry ──


def register_nested_model(model_cls: type[WireMessage]) -> None:
    """Register a WireMessage subclass as a serializable nested type.

    Wraps the model's ``serialize``/``deserialize`` into the type
    registry so it can be used as an inner type of ``Conditional[T]``.
    """

    async def _read_nested(r):
        return await model_cls.deserialize(ReadContext(reader=r, version=0))

    def _write_nested(v, w):
        return v.serialize(WriteContext(writer=w, version=0))

    register_type(model_cls, _read_nested, _write_nested)


# ── Version-constrained fields ──


class VersionFieldInfo(FieldInfo):  # type: ignore[misc]
    """FieldInfo with Nix protocol version constraints."""

    def __init__(self, **kwargs):
        self.min_version: int | None = kwargs.pop("min_version", None)  # type: ignore[assignment]
        self.max_version: int | None = kwargs.pop("max_version", None)  # type: ignore[assignment]
        super().__init__(**kwargs)


def WireField(  # noqa: N802
    default: Any = ...,
    *,
    default_factory: Any = None,
    min_version: int | None = None,
    max_version: int | None = None,
    **kwargs: Any,
) -> Any:
    """A Pydantic Field with Nix protocol version requirements.

    Usage::

        times_built: int = WireField(default=0, min_version=proto(1, 29))
        legacy_field: str = WireField(default="", max_version=proto(1, 27))
    """
    return VersionFieldInfo(
        default=default,
        default_factory=default_factory,
        min_version=min_version,  # type: ignore[arg-type]
        max_version=max_version,  # type: ignore[arg-type]
        **kwargs,
    )


# ── Field collection ──


def _wire_fields(cls: type[BaseModel], version: int | None = None) -> list[tuple[str, type, bool]]:
    """Return (name, type, is_conditional) triples in declaration order.

    ClassVar fields are skipped.

    Fields with ``min_version`` or ``max_version`` constraints
    (set via :func:`WireField`) are filtered against the provided
    ``version``, enabling protocol-version-dependent serde.

    For generic types (e.g. ``set[str]``) the resolved annotation type is
    returned (e.g. ``set``) rather than the parameterized form, so the
    registry lookup can match on the bare type.

    For ``Conditional[T]``, the inner type ``T`` is returned with
    ``is_conditional=True``.
    """
    hints = get_type_hints(cls, include_extras=True)
    result = []
    for name in cls.model_fields:
        field = cls.model_fields[name]
        min_v = getattr(field, "min_version", None)
        max_v = getattr(field, "max_version", None)
        if min_v is not None and version is not None and version < min_v:
            continue
        if max_v is not None and version is not None and version > max_v:
            continue

        ann = hints.get(name)
        if ann is None:
            continue
        origin = getattr(ann, "__origin__", None)
        if origin is ClassVar:
            continue
        if origin is Conditional:
            # Extract inner type for registry lookup; mark as conditional
            result.append((name, ann.__args__[0], True))
        elif origin is not None:
            # Generic type like set[str] → use origin (set)
            result.append((name, origin, False))
        else:
            # Plain type like int, str, bool
            result.append((name, ann, False))
    return result


# ── Base class ──


class WireMessage(BaseModel):
    """Pydantic base class with auto-generated Nix daemon protocol serde.

    Usage:
        class FooResponse(WireMessage):
            valid: int   # uint64 on the wire
            path: str    # length-prefixed UTF-8
    """

    model_config = {"arbitrary_types_allowed": True}

    async def serialize(self, ctx: WriteContext) -> None:
        """Write all non-ClassVar fields in declaration order."""
        for name, ann, is_conditional in _wire_fields(type(self), version=ctx.version):
            if is_conditional:
                val = getattr(self, name)
                ctx.writer.write_uint64(1 if val.is_present else 0)
                if val.is_present:
                    inner_writer = _WRITERS.get(ann)
                    if inner_writer is not None:
                        result = inner_writer(val.value, ctx.writer)
                        if asyncio.iscoroutine(result):
                            await result
                    elif isinstance(ann, type) and issubclass(ann, WireMessage):
                        result = val.value.serialize(ctx)
                        if asyncio.iscoroutine(result):
                            await result
                    else:
                        raise TypeError(f"No writer registered for {ann} in {type(self).__name__}.{name}")
                continue

            writer = _WRITERS.get(ann)
            if writer is not None:
                result = writer(getattr(self, name), ctx.writer)
                if asyncio.iscoroutine(result):
                    await result
            elif isinstance(ann, type) and issubclass(ann, WireMessage):
                result = getattr(self, name).serialize(ctx)
                if asyncio.iscoroutine(result):
                    await result
            else:
                raise TypeError(f"No writer registered for {ann} in {type(self).__name__}.{name}")

    @classmethod
    async def deserialize(cls, ctx: ReadContext):
        """Read all non-ClassVar fields in declaration order."""
        kwargs = {}
        for name, ann, is_conditional in _wire_fields(cls, version=ctx.version):
            if is_conditional:
                reader = _READERS.get(ann)
                if reader is not None:
                    valid = await ctx.reader.read_uint64()
                    if valid:
                        kwargs[name] = Conditional(await reader(ctx.reader))
                    else:
                        kwargs[name] = Conditional(None)
                elif isinstance(ann, type) and issubclass(ann, WireMessage):
                    valid = await ctx.reader.read_uint64()
                    if valid:
                        inner = await ann.deserialize(ReadContext(reader=ctx.reader, version=ctx.version))
                        kwargs[name] = Conditional(inner)
                    else:
                        kwargs[name] = Conditional(None)
                else:
                    raise TypeError(f"No reader registered for {ann} in {cls.__name__}.{name}")
                continue

            reader = _READERS.get(ann)
            if reader is not None:
                kwargs[name] = await reader(ctx.reader)
            elif isinstance(ann, type) and issubclass(ann, WireMessage):
                kwargs[name] = await ann.deserialize(ReadContext(reader=ctx.reader, version=ctx.version))
            else:
                raise TypeError(f"No reader registered for {ann} in {cls.__name__}.{name}")
        return cls(**kwargs)


class WireStorePath(WireMessage):
    """A store path on the Nix daemon wire protocol.

    Wire format: single length-prefixed UTF-8 string.

    Usage::

        class MyRequest(WireMessage):
            path: WireStorePath  # auto-detected as WireMessage subtype
    """

    path: str

    def __str__(self) -> str:
        return self.path

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WireStorePath):
            return NotImplemented
        return self.path == other.path


class WireBuildResult(WireMessage):
    """Nix daemon protocol BuildResult.

    Fields present based on protocol version:
    - All versions: status, error_msg
    - >= 1.29: times_built, is_non_deterministic, start_time, stop_time
    - >= 1.37: cpu_user, cpu_system (Conditional[int])
    - >= 1.28: built_outputs (dict[str, str])
    """

    status: int
    error_msg: str

    # Protocol 1.29 fields
    times_built: int = WireField(default=0, min_version=proto(1, 29))
    is_non_deterministic: int = WireField(default=0, min_version=proto(1, 29))
    start_time: int = WireField(default=0, min_version=proto(1, 29))
    stop_time: int = WireField(default=0, min_version=proto(1, 29))

    # Protocol 1.37 fields
    cpu_user: Conditional[int] = WireField(default=Conditional(None), min_version=proto(1, 37))
    cpu_system: Conditional[int] = WireField(default=Conditional(None), min_version=proto(1, 37))

    # Protocol 1.28 fields
    built_outputs: dict[str, str] = WireField(default_factory=dict, min_version=proto(1, 28))


class WireBuildDerivationResponse(WireMessage):
    """BuildDerivation response — a BuildResult wrapped in the response body."""

    result: WireBuildResult
