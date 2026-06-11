"""Minimal binary serialization prototype using Pydantic.

Provides a type registry for primitive binary (de)serialization and a
Pydantic base class that can round-trip to/from bytes via the registry.

This is a prototype — not used by production code paths.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from typing import Any, Self

from pydantic import BaseModel

ReaderFunc = Callable[[asyncio.StreamReader], Any]  # coroutine
WriterFunc = Callable[[Any, asyncio.StreamWriter], None]

_BINARY_REGISTRY: dict[type, tuple[ReaderFunc, WriterFunc]] = {}


def register_binary_type(py_type: type, reader: ReaderFunc, writer: WriterFunc) -> None:
    """Register a reader/writer pair for a Python type."""
    _BINARY_REGISTRY[py_type] = (reader, writer)


# ── Primitive handlers ──


async def _read_uint64(r: asyncio.StreamReader) -> int:
    return struct.unpack("<Q", await r.readexactly(8))[0]


def _write_uint64(v: int, w: asyncio.StreamWriter) -> None:
    w.write(struct.pack("<Q", v))


async def _read_bytes(r: asyncio.StreamReader) -> bytes:
    n = await _read_uint64(r)
    return await r.readexactly(n)


def _write_bytes(v: bytes, w: asyncio.StreamWriter) -> None:
    w.write(struct.pack("<Q", len(v)))
    w.write(v)


async def _read_string(r: asyncio.StreamReader) -> str:
    return (await _read_bytes(r)).decode("utf-8")


def _write_string(v: str, w: asyncio.StreamWriter) -> None:
    _write_bytes(v.encode("utf-8"), w)


register_binary_type(int, _read_uint64, _write_uint64)
register_binary_type(bytes, _read_bytes, _write_bytes)


async def _read_bool(r: asyncio.StreamReader) -> bool:
    return bool(await _read_uint64(r))


def _write_bool(v: bool, w: asyncio.StreamWriter) -> None:
    _write_uint64(1 if v else 0, w)


register_binary_type(bool, _read_bool, _write_bool)
register_binary_type(str, _read_string, _write_string)


# ── Pydantic base class ──


class BinaryProtocolMessage(BaseModel):
    """Base class for Pydantic models that can (de)serialize to binary wire format.

    Uses the global ``_BINARY_REGISTRY`` to look up reader/writer functions
    for each field's type annotation. Fields must have types registered in
    the registry (``int``, ``str``, ``bytes``, ``bool`` by default).
    """

    @classmethod
    async def from_stream(cls, r: asyncio.StreamReader) -> Self:
        """Deserialize an instance from a binary stream.

        Reads fields in the order they are declared on the model, using
        the registered reader for each field's type annotation.
        """
        kwargs: dict[str, Any] = {}
        for name, field in cls.model_fields.items():
            reader, _ = _BINARY_REGISTRY[field.annotation]  # type: ignore[index]
            kwargs[name] = await reader(r)
        return cls(**kwargs)

    async def to_stream(self, w: asyncio.StreamWriter) -> None:
        """Serialize this instance to a binary stream.

        Writes fields in declaration order using the registered writer
        for each field's type annotation.
        """
        for name, field in type(self).model_fields.items():
            _, writer = _BINARY_REGISTRY[field.annotation]  # type: ignore[index]
            writer(getattr(self, name), w)
