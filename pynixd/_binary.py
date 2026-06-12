"""Declarative binary serialization for Nix daemon protocol types.

Pydantic models inherit from ``WireMessage`` to auto-generate
serialize()/deserialize() from type annotations.

Class variables (ClassVar[int]) are skipped — they're protocol constants
like op codes that are written/read by the operation layer, not the message body.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass
from typing import Any, ClassVar, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, model_serializer, model_validator
from pydantic import Field as PydanticField
from pydantic_core import PydanticUndefined

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


async def _read_bool(r):
    return bool(await r.read_uint64())


register_type(
    bool,
    reader=_read_bool,
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


# ── Helpers ──


def _find_reader(ann: type, version: int = 0) -> Any:
    """Look up a reader for a wire type, including nested WireMessage."""
    reader = _READERS.get(ann)
    if reader is not None:
        return reader
    if isinstance(ann, type) and issubclass(ann, WireMessage):

        async def _read_nested(r):
            return await ann.deserialize(ReadContext(reader=r, version=version))

        return _read_nested
    raise TypeError(f"No reader registered for {ann}")


async def _write_value(val: Any, ann: type, ctx: WriteContext) -> None:
    """Write a value to the wire using the registered writer or nested serialization."""
    writer = _WRITERS.get(ann)
    if writer is not None:
        result = writer(val, ctx.writer)
        if asyncio.iscoroutine(result):
            await result
    elif isinstance(ann, type) and issubclass(ann, WireMessage):
        result = val.serialize(ctx)
        if asyncio.iscoroutine(result):
            await result
    else:
        raise TypeError(f"No writer for {ann}")


# ── Version-constrained fields ──


@dataclass(frozen=True)
class VersionMeta:
    """Protocol version constraint for a wire field."""

    min_version: int | None = None
    max_version: int | None = None
    wire_depends_on: Callable | None = None


def WireField(  # noqa: N802
    default: Any = PydanticUndefined,
    *,
    default_factory: Any = None,
    min_version: int | None = None,
    max_version: int | None = None,
    wire_depends_on: Callable | None = None,
    **kwargs: Any,
) -> Any:
    """A Pydantic Field with Nix protocol version requirements.

    Usage::

        times_built: int = WireField(default=0, min_version=proto(1, 29))
        legacy_field: str = WireField(default="", max_version=proto(1, 27))
    """
    if default is not PydanticUndefined:
        kwargs.setdefault("default", default)
    if default_factory is not None:
        kwargs["default_factory"] = default_factory

    field_info = PydanticField(**kwargs)
    field_info.metadata.append(VersionMeta(min_version, max_version, wire_depends_on))
    return field_info


# ── Field collection ──


def _wire_fields(cls: type[BaseModel], version: int | None = None) -> list[tuple[str, type, Callable | None]]:
    """Return (name, type, wire_depends_on) tuples in declaration order.

    ClassVar fields are skipped.

    Fields with ``min_version`` or ``max_version`` constraints
    (set via :func:`WireField`) are filtered against the provided
    ``version``, enabling protocol-version-dependent serde.

    For generic types (e.g. ``set[str]``) the resolved annotation type is
    returned (e.g. ``set``) rather than the parameterized form, so the
    registry lookup can match on the bare type.
    """
    hints = get_type_hints(cls, include_extras=True)
    result = []
    for name in cls.model_fields:
        field = cls.model_fields[name]
        version_meta = next((m for m in field.metadata if isinstance(m, VersionMeta)), None)
        if version_meta is not None:
            if version_meta.min_version is not None and version is not None and version < version_meta.min_version:
                continue
            if version_meta.max_version is not None and version is not None and version > version_meta.max_version:
                continue

        ann = hints.get(name)
        if ann is None:
            continue
        origin = get_origin(ann)
        if origin is ClassVar:
            continue

        # Handle Optional[T] → T (strip None from union types)
        if isinstance(ann, types.UnionType):
            non_none = tuple(a for a in get_args(ann) if a is not type(None))
            if len(non_none) == 1:
                ann = non_none[0]
                origin = get_origin(ann)
            # if 0 or >1 non-None args, leave ann as-is (will fail registry lookup)

        wire_depends_on = version_meta.wire_depends_on if version_meta else None

        resolved = origin if origin is not None else ann
        result.append((name, resolved, wire_depends_on))
    return result


# ── Base class ──


class WireMessage(BaseModel):
    """Pydantic base class with auto-generated Nix daemon protocol serde.

    Usage:
        class FooResponse(WireMessage):
            valid: int   # uint64 on the wire
            path: str    # length-prefixed UTF-8
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    async def serialize(self, ctx: WriteContext) -> None:
        """Write all non-ClassVar fields in declaration order."""
        for name, ann, wire_depends_on in _wire_fields(type(self), version=ctx.version):
            # Check wire_depends_on — skip field if dependency is False
            if wire_depends_on is not None and not wire_depends_on(self):
                continue

            val = getattr(self, name)
            await _write_value(val, ann, ctx)

    @classmethod
    async def deserialize(cls, ctx: ReadContext):
        """Read all non-ClassVar fields in declaration order."""
        obj = cls.__new__(cls)
        # Initialize Pydantic internals (bypassed __init__)
        object.__setattr__(obj, "__pydantic_fields_set__", set())
        object.__setattr__(obj, "__pydantic_extra__", None)
        object.__setattr__(obj, "__pydantic_private__", None)
        # Set defaults for version-gated and conditional fields
        for name, field in cls.model_fields.items():
            if field.default is not PydanticUndefined:
                object.__setattr__(obj, name, field.default)
            elif field.default_factory is not None:
                object.__setattr__(obj, name, field.default_factory())  # pyright: ignore[reportCallIssue]

        for name, ann, wire_depends_on in _wire_fields(cls, version=ctx.version):
            # Check wire_depends_on — skip field if dependency is False
            if wire_depends_on is not None and not wire_depends_on(obj):
                continue

            reader = _find_reader(ann, version=ctx.version)
            object.__setattr__(obj, name, await reader(ctx.reader))
            obj.__pydantic_fields_set__.add(name)

        return obj

    def to_json(self, **kwargs) -> str:
        """Serialize to JSON string.

        Uses Pydantic's ``model_dump_json``.
        """
        return self.model_dump_json(**kwargs)

    @classmethod
    def from_json(cls, json_data: str, **kwargs) -> WireMessage:
        """Deserialize from JSON string.

        Uses Pydantic's ``model_validate_json``.
        """
        return cls.model_validate_json(json_data, **kwargs)  # type: ignore[return-value]


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

    @model_serializer
    def ser_model(self) -> str:
        """Serialize WireStorePath as a plain string in JSON."""
        return self.path

    @model_validator(mode="before")
    @classmethod
    def from_str(cls, data: Any) -> Any:
        """Deserialize WireStorePath from a plain string in JSON."""
        if isinstance(data, str):
            return {"path": data}
        if isinstance(data, cls):
            return data
        return data


class WireOptMicroseconds(WireMessage):
    """Optional microseconds — [tag uint64][microseconds uint64 if tag=1].

    Wire format: tag (uint64) — 1 means present, then uint64 value follows.
    0 means absent, nothing follows.

    JSON format: present → integer, absent → null.

    Attributes:
        value: int | None — the microseconds value, or None if absent.
    """

    value: int | None = None

    @property
    def is_present(self) -> bool:
        return self.value is not None

    @classmethod
    async def deserialize(cls, ctx: ReadContext):
        tag = await ctx.reader.read_uint64()
        obj = cls.__new__(cls)
        object.__setattr__(obj, "__pydantic_fields_set__", set())
        object.__setattr__(obj, "__pydantic_extra__", None)
        object.__setattr__(obj, "__pydantic_private__", None)
        if tag == 1:
            object.__setattr__(obj, "value", await ctx.reader.read_uint64())
            obj.__pydantic_fields_set__.add("value")
        else:
            object.__setattr__(obj, "value", None)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        if self.value is not None:
            ctx.writer.write_uint64(1)
            ctx.writer.write_uint64(self.value)
        else:
            ctx.writer.write_uint64(0)

    @model_serializer
    def _ser(self) -> int | None:
        return self.value

    @model_validator(mode="before")
    @classmethod
    def _val(cls, data: Any) -> Any:
        if isinstance(data, int | None):
            return {"value": data}
        if isinstance(data, cls):
            return data
        return data


class WireBuildResult(WireMessage):
    """Nix daemon protocol BuildResult.

    Fields present based on protocol version:
    - All versions: status, error_msg
    - >= 1.29: times_built, is_non_deterministic, start_time, stop_time
    - >= 1.37: cpu_user, cpu_system
    - >= 1.28: built_outputs (dict[str, str])
    """

    status: int
    error_msg: str

    # Protocol 1.29 fields
    times_built: int | None = WireField(default=None, min_version=proto(1, 29))
    is_non_deterministic: int | None = WireField(default=None, min_version=proto(1, 29))
    start_time: int | None = WireField(default=None, min_version=proto(1, 29))
    stop_time: int | None = WireField(default=None, min_version=proto(1, 29))

    # Protocol 1.37 fields
    cpu_user: WireOptMicroseconds = WireField(default_factory=WireOptMicroseconds, min_version=proto(1, 37))
    cpu_system: WireOptMicroseconds = WireField(default_factory=WireOptMicroseconds, min_version=proto(1, 37))

    # Protocol 1.28 fields
    built_outputs: dict[str, str] | None = WireField(default=None, min_version=proto(1, 28))


class WireBuildDerivationResponse(WireMessage):
    """BuildDerivation response — a BuildResult wrapped in the response body."""

    result: WireBuildResult


class WirePathInfo(WireMessage):
    """Wire mirror of UnkeyedValidPathInfo."""

    deriver: str
    nar_hash: str
    references: set[str]
    registration_time: int
    nar_size: int
    ultimate: int
    sigs: set[str]
    ca: str


class WireQueryPathInfoResponse(WireMessage):
    """QueryPathInfo response — info depends on valid flag."""

    valid: bool
    info: WirePathInfo | None = WireField(
        default=None,
        wire_depends_on=lambda self: self.valid,
    )
