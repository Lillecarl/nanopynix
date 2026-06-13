"""Declarative binary serialization for Nix daemon protocol types.

Pydantic models inherit from ``WireMessage`` to auto-generate
to_writer()/from_reader() from type annotations.

Class variables (ClassVar[int]) are skipped — they're protocol constants
like op codes that are written/read by the operation layer, not the message body.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, ClassVar, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField
from pydantic_core import PydanticUndefined

from ..types.context import ReadContext, WriteContext

# ── Helpers ──


def _find_reader(ann: type, version: int = 0) -> Any:
    """Look up a reader for a wire type."""
    from .types import WireString  # lazy: break circular import

    # Primitives
    if ann is int:
        return lambda r: r.read_uint64()
    if ann is str:
        return lambda r: r.read_string(str)
    if ann is bool:
        return lambda r: r.read_bool()
    if ann is bytes:
        return lambda r: r.read_bytes()

    origin = get_origin(ann)
    args = get_args(ann)

    # Optional[T] — strip None, delegate to inner
    if origin is types.UnionType:
        non_none = tuple(a for a in args if a is not type(None))
        if len(non_none) == 1:
            return _find_reader(non_none[0], version)

    # -- list generics --
    if origin is list:
        elem = _find_reader(args[0], version)

        async def _read_list(r):
            n = await r.read_uint64()
            return [await elem(r) for _ in range(n)]

        return _read_list

    # -- set generics --
    if origin is set:
        elem = _find_reader(args[0], version)

        async def _read_set(r):
            n = await r.read_uint64()
            return {await elem(r) for _ in range(n)}

        return _read_set

    # -- dict generics --
    if origin is dict:
        k_reader = _find_reader(args[0], version)
        v_reader = _find_reader(args[1], version)

        async def _read_dict(r):
            n = await r.read_uint64()
            d = {}
            for _ in range(n):
                key = await k_reader(r)
                val = await v_reader(r)
                d[key] = val
            return d

        return _read_dict

    # WireString — read one string, delegate to from_string + model_construct
    if isinstance(ann, type) and issubclass(ann, WireString):

        async def _read_string(r):
            assert issubclass(ann, WireString)
            raw = await r.read_string(str)
            data = ann.from_string(raw)
            if isinstance(data, str):
                fields = list(ann.model_fields.keys())
                data = {fields[0]: data} if len(fields) == 1 else {}
            return ann.model_construct(**data)

        return _read_string

    # WireMessage subclass
    if isinstance(ann, type) and issubclass(ann, WireMessage):

        async def _read_nested(r):
            return await ann.from_reader(ReadContext(reader=r, version=version))

        return _read_nested

    # IntEnum — read uint64, construct via enum
    if isinstance(ann, type) and issubclass(ann, IntEnum):

        async def _read_enum(r):
            return ann(await r.read_uint64())

        return _read_enum

    raise TypeError(f"No reader for {ann}")


async def _write_value(val: Any, ann: type, ctx: WriteContext) -> None:
    """Write a value to the wire using primitives, generics, or nested serialization."""
    from .types import WireString  # lazy: break circular import

    # Primitives
    if ann is int:
        ctx.writer.write_uint64(val)
        return None
    if ann is str:
        ctx.writer.write_string(val)
        return None
    if ann is bool:
        ctx.writer.write_bool(val)
        return None
    if ann is bytes:
        ctx.writer.write_bytes(val)
        return None

    origin = get_origin(ann)
    args = get_args(ann)

    # Optional[T] — delegate to inner type (val should never be None on the wire)
    if origin is types.UnionType:
        non_none = tuple(a for a in args if a is not type(None))
        if len(non_none) == 1:
            return await _write_value(val, non_none[0], ctx)

    # -- list generics --
    if origin is list:
        ctx.writer.write_uint64(len(val))
        for item in val:
            await _write_value(item, args[0], ctx)
        return None

    # -- set generics --
    if origin is set:
        ctx.writer.write_uint64(len(val))
        for item in val:
            await _write_value(item, args[0], ctx)
        return None

    # -- dict generics --
    if origin is dict:
        ctx.writer.write_uint64(len(val))
        for k, v in val.items():
            await _write_value(k, args[0], ctx)
            await _write_value(v, args[1], ctx)
        return None

    # WireString — write str(self) as a single wire string
    if isinstance(ann, type) and issubclass(ann, WireString):
        ctx.writer.write_string(str(val))
        return None

    # WireMessage subclass
    if isinstance(ann, type) and issubclass(ann, WireMessage):
        result = val.to_writer(ctx)
        if asyncio.iscoroutine(result):
            await result
        return None

    # IntEnum — write its int value
    if isinstance(ann, type) and issubclass(ann, IntEnum):
        ctx.writer.write_uint64(val.value)
        return None

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
    """Return (name, raw_annotation, wire_depends_on) tuples in declaration order.

    ClassVar fields are skipped.

    Fields with ``min_version`` or ``max_version`` constraints
    (set via :func:`WireField`) are filtered against the provided
    ``version``, enabling protocol-version-dependent serde.

    Raw annotations (e.g. ``set[str]``, ``int | None``) are passed through
    without stripping generics or Optional wrappers.  The resolution is
    handled downstream by ``_find_reader`` / ``_write_value``.
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
        if get_origin(ann) is ClassVar:
            continue

        wire_depends_on = version_meta.wire_depends_on if version_meta else None
        result.append((name, ann, wire_depends_on))
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

    def __hash__(self) -> int:
        return hash(tuple(getattr(self, f) for f in self.__class__.model_fields))

    async def to_writer(self, ctx: WriteContext) -> None:
        """Write all non-ClassVar fields in declaration order."""
        for name, ann, wire_depends_on in _wire_fields(type(self), version=ctx.version):
            # Check wire_depends_on — skip field if dependency is False
            if wire_depends_on is not None and not wire_depends_on(self):
                continue

            val = getattr(self, name)
            await _write_value(val, ann, ctx)

    @classmethod
    async def from_reader(cls, ctx: ReadContext):
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
        return cls.model_validate_json(json_data, **kwargs)
