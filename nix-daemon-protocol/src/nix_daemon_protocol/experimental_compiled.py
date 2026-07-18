"""Experimental import-time compiler for declarative daemon codecs.

This is deliberately opt-in: ``WireModel`` continues to use the small,
inspectable generic implementation in :mod:`nix_daemon_protocol.wire_message`.
``compile_codec`` demonstrates that the declarative schema can instead be
lowered to one concrete coroutine pair per model/protocol-version combination.

The generated functions contain direct attribute access and field calls.  Type
dispatch and version filtering happen while compiling, rather than once per
field in every message.  Custom codecs such as ``WireString`` and ``WireLogs``
remain the source of truth and are called unchanged.
"""

from __future__ import annotations

import functools
import types
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import Any, get_args, get_origin

from pydantic_core import PydanticUndefined

from .constants import proto_str
from .context import ReadContext, WriteContext
from .exceptions import UnsupportedProtocolVersion
from .logging import deserialization_scope
from .wire_integer import WireUInt64
from .wire_message import WireModel, _wire_fields
from .wire_ops import WireRequest
from .wire_scalar import WireScalar

Writer = Callable[[Any, WriteContext], Awaitable[None]]
Reader = Callable[[ReadContext], Awaitable[Any]]


class CompiledCodec:
    """A generated serializer/deserializer pair for one model and version."""

    def __init__(
        self,
        write: Callable[[WireModel, WriteContext], Awaitable[None]],
        read: Callable[[ReadContext], Awaitable[WireModel]],
        write_source: str,
        read_source: str,
    ) -> None:
        self.write = write
        self.read = read
        self.write_source = write_source
        self.read_source = read_source


class _SourceEmitter:
    """Lower wire annotations into statements in one generated coroutine."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.adapters: list[Writer | Reader] = []
        self.codecs: list[CompiledCodec] = []
        self.enums: list[type[IntEnum]] = []
        self._counter = 0

    def name(self, prefix: str) -> str:
        """Return a collision-free local variable name."""
        result = f"_{prefix}_{self._counter}"
        self._counter += 1
        return result

    def write_value(self, lines: list[str], annotation: type, value: str, indent: int) -> None:
        """Emit direct write statements for one value expression."""
        from .wire_string import WireString

        pad = " " * indent
        if annotation is int:
            lines.append(f"{pad}ctx.writer.write_uint64({value})")
            return
        if isinstance(annotation, type) and issubclass(annotation, WireUInt64):
            lines.append(f"{pad}ctx.writer.write_uint64({value})")
            return
        if annotation is str:
            lines.append(f"{pad}ctx.writer.write_string({value})")
            return
        if annotation is bool:
            lines.append(f"{pad}ctx.writer.write_bool({value})")
            return
        if annotation is bytes:
            lines.append(f"{pad}ctx.writer.write_bytes({value})")
            return
        if isinstance(annotation, type) and issubclass(annotation, IntEnum):
            lines.append(f"{pad}ctx.writer.write_uint64({value}.value)")
            return
        if isinstance(annotation, type) and issubclass(annotation, WireScalar):
            lines.append(f"{pad}ctx.writer.write_string({value})")
            return

        origin = get_origin(annotation)
        arguments = get_args(annotation)
        if origin is types.UnionType:
            non_none = tuple(arg for arg in arguments if arg is not type(None))
            if len(non_none) == 1:
                if non_none[0].__name__ == "StorePath":
                    lines.append(f"{pad}if {value} is None:")
                    lines.append(f'{pad}    ctx.writer.write_string("")')
                    lines.append(f"{pad}else:")
                    self.write_value(lines, non_none[0], value, indent + 4)
                    return
                self.write_value(lines, non_none[0], value, indent)
                return
        if origin is list or origin is set:
            item = self.name("item")
            lines.append(f"{pad}ctx.writer.write_uint64(len({value}))")
            lines.append(f"{pad}for {item} in {value}:")
            self.write_value(lines, arguments[0], item, indent + 4)
            return
        if origin is dict:
            key = self.name("key")
            item = self.name("item")
            lines.append(f"{pad}ctx.writer.write_uint64(len({value}))")
            lines.append(f"{pad}for {key}, {item} in {value}.items():")
            self.write_value(lines, arguments[0], key, indent + 4)
            self.write_value(lines, arguments[1], item, indent + 4)
            return
        if isinstance(annotation, type) and issubclass(annotation, WireString):
            field_names = tuple(annotation.model_fields)
            if annotation.to_str is WireString.to_str and len(field_names) == 1:
                lines.append(f"{pad}ctx.writer.write_string({value}.{field_names[0]})")
            else:
                lines.append(f"{pad}ctx.writer.write_string(str({value}))")
            return
        if isinstance(annotation, type) and issubclass(annotation, WireModel):
            if _can_compile(annotation):
                codec_index = len(self.codecs)
                self.codecs.append(compile_codec(annotation, self.version))
                lines.append(f"{pad}await codecs[{codec_index}].write({value}, ctx)")
                return
            adapter_index = len(self.adapters)
            self.adapters.append(_writer_for(annotation, self.version))
            lines.append(f"{pad}await adapters[{adapter_index}]({value}, ctx)")
            return
        raise TypeError(f"No writer for {annotation}")

    def read_value(self, lines: list[str], annotation: type, target: str, indent: int) -> None:
        """Emit direct read statements assigning to *target*."""
        pad = " " * indent
        if annotation is int:
            lines.append(f"{pad}{target} = await ctx.reader.read_uint64()")
            return
        if isinstance(annotation, type) and issubclass(annotation, WireUInt64):
            adapter_index = len(self.adapters)
            self.adapters.append(annotation)
            lines.append(f"{pad}{target} = adapters[{adapter_index}](await ctx.reader.read_uint64())")
            return
        if annotation is str:
            lines.append(f"{pad}{target} = await ctx.reader.read_string(str)")
            return
        if annotation is bool:
            lines.append(f"{pad}{target} = await ctx.reader.read_bool()")
            return
        if annotation is bytes:
            lines.append(f"{pad}{target} = await ctx.reader.read_bytes()")
            return
        if isinstance(annotation, type) and issubclass(annotation, IntEnum):
            enum_index = len(self.enums)
            self.enums.append(annotation)
            lines.append(f"{pad}{target} = enums[{enum_index}](await ctx.reader.read_uint64())")
            return
        if isinstance(annotation, type) and issubclass(annotation, WireScalar):
            adapter_index = len(self.adapters)
            self.adapters.append(annotation.from_wire)
            lines.append(f"{pad}{target} = adapters[{adapter_index}](await ctx.reader.read_string(str))")
            return

        origin = get_origin(annotation)
        arguments = get_args(annotation)
        if origin is types.UnionType:
            non_none = tuple(arg for arg in arguments if arg is not type(None))
            if len(non_none) == 1:
                self.read_value(lines, non_none[0], target, indent)
                return
        if origin is list:
            item = self.name("item")
            lines.append(f"{pad}{target} = []")
            lines.append(f"{pad}for _ in range(await ctx.reader.read_uint64()):")
            self.read_value(lines, arguments[0], item, indent + 4)
            lines.append(f"{pad}    {target}.append({item})")
            return
        if origin is set:
            item = self.name("item")
            lines.append(f"{pad}{target} = set()")
            lines.append(f"{pad}for _ in range(await ctx.reader.read_uint64()):")
            self.read_value(lines, arguments[0], item, indent + 4)
            lines.append(f"{pad}    {target}.add({item})")
            return
        if origin is dict:
            key = self.name("key")
            item = self.name("item")
            lines.append(f"{pad}{target} = {{}}")
            lines.append(f"{pad}for _ in range(await ctx.reader.read_uint64()):")
            self.read_value(lines, arguments[0], key, indent + 4)
            self.read_value(lines, arguments[1], item, indent + 4)
            lines.append(f"{pad}    {target}[{key}] = {item}")
            return
        if isinstance(annotation, type) and issubclass(annotation, WireModel):
            if _can_compile(annotation):
                codec_index = len(self.codecs)
                self.codecs.append(compile_codec(annotation, self.version))
                lines.append(f"{pad}{target} = await codecs[{codec_index}].read(ctx)")
                return
            adapter_index = len(self.adapters)
            self.adapters.append(_reader_for(annotation, self.version))
            lines.append(f"{pad}{target} = await adapters[{adapter_index}](ctx)")
            return
        raise TypeError(f"No reader for {annotation}")


def _can_compile(model: type[WireModel]) -> bool:
    """Whether ``model`` has declarative fields plus an understood prelude."""
    return (
        model.to_writer is WireModel.to_writer and model.from_reader.__func__ is WireModel.from_reader.__func__
    ) or issubclass(model, WireRequest)


async def _write_request_prelude(value: WireRequest, ctx: WriteContext) -> None:
    """Write the only non-declarative portion shared by all requests."""
    if ctx.version and ctx.version < value.min_protocol:
        raise UnsupportedProtocolVersion(
            f"{value.name} requires daemon protocol >= {proto_str(value.min_protocol)}, got {proto_str(ctx.version)}",
        )
    ctx.writer.write_uint64(value.op)


@functools.cache
def _writer_for(annotation: type, version: int) -> Writer:
    """Specialize a write closure once for an annotation/version pair."""
    from .wire_string import WireString

    if annotation is int:

        async def write_int(value: int, ctx: WriteContext) -> None:
            ctx.writer.write_uint64(value)

        return write_int
    if annotation is str:

        async def write_str(value: str, ctx: WriteContext) -> None:
            ctx.writer.write_string(value)

        return write_str
    if annotation is bool:

        async def write_bool(value: bool, ctx: WriteContext) -> None:
            ctx.writer.write_bool(value)

        return write_bool
    if annotation is bytes:

        async def write_bytes(value: bytes, ctx: WriteContext) -> None:
            ctx.writer.write_bytes(value)

        return write_bytes
    if isinstance(annotation, type) and issubclass(annotation, IntEnum):

        async def write_enum(value: IntEnum, ctx: WriteContext) -> None:
            ctx.writer.write_uint64(value.value)

        return write_enum

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is types.UnionType:
        non_none = tuple(arg for arg in arguments if arg is not type(None))
        if len(non_none) == 1:
            writer = _writer_for(non_none[0], version)
            if non_none[0].__name__ == "StorePath":

                async def write_optional_store_path(value: Any, ctx: WriteContext) -> None:
                    if value is None:
                        ctx.writer.write_string("")
                    else:
                        await writer(value, ctx)

                return write_optional_store_path
            return writer
    if origin is list:
        writer = _writer_for(arguments[0], version)

        async def write_list(value: list[Any], ctx: WriteContext) -> None:
            ctx.writer.write_uint64(len(value))
            for item in value:
                await writer(item, ctx)

        return write_list
    if origin is set:
        writer = _writer_for(arguments[0], version)

        async def write_set(value: set[Any], ctx: WriteContext) -> None:
            ctx.writer.write_uint64(len(value))
            for item in value:
                await writer(item, ctx)

        return write_set
    if origin is dict:
        key_writer = _writer_for(arguments[0], version)
        value_writer = _writer_for(arguments[1], version)

        async def write_dict(value: dict[Any, Any], ctx: WriteContext) -> None:
            ctx.writer.write_uint64(len(value))
            for key, item in value.items():
                await key_writer(key, ctx)
                await value_writer(item, ctx)

        return write_dict
    if isinstance(annotation, type) and issubclass(annotation, WireString):

        async def write_wire_string(value: WireString, ctx: WriteContext) -> None:
            ctx.writer.write_string(str(value))

        return write_wire_string
    if isinstance(annotation, type) and issubclass(annotation, WireModel):
        if _can_compile(annotation):
            codec = compile_codec(annotation, version)
            return codec.write

        async def write_custom_model(value: WireModel, ctx: WriteContext) -> None:
            await value.to_writer(ctx)

        return write_custom_model
    raise TypeError(f"No writer for {annotation}")


@functools.cache
def _reader_for(annotation: type, version: int) -> Reader:
    """Specialize a read closure once for an annotation/version pair."""
    from .wire_string import WireString

    if annotation is int:

        async def read_int(ctx: ReadContext) -> int:
            return await ctx.reader.read_uint64()

        return read_int
    if annotation is str:

        async def read_str(ctx: ReadContext) -> str:
            return await ctx.reader.read_string(str)

        return read_str
    if annotation is bool:

        async def read_bool(ctx: ReadContext) -> bool:
            return await ctx.reader.read_bool()

        return read_bool
    if annotation is bytes:

        async def read_bytes(ctx: ReadContext) -> bytes:
            return await ctx.reader.read_bytes()

        return read_bytes
    if isinstance(annotation, type) and issubclass(annotation, IntEnum):

        async def read_enum(ctx: ReadContext) -> IntEnum:
            return annotation(await ctx.reader.read_uint64())

        return read_enum

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is types.UnionType:
        non_none = tuple(arg for arg in arguments if arg is not type(None))
        if len(non_none) == 1:
            return _reader_for(non_none[0], version)
    if origin is list:
        reader = _reader_for(arguments[0], version)

        async def read_list(ctx: ReadContext) -> list[Any]:
            return [await reader(ctx) for _ in range(await ctx.reader.read_uint64())]

        return read_list
    if origin is set:
        reader = _reader_for(arguments[0], version)

        async def read_set(ctx: ReadContext) -> set[Any]:
            return {await reader(ctx) for _ in range(await ctx.reader.read_uint64())}

        return read_set
    if origin is dict:
        key_reader = _reader_for(arguments[0], version)
        value_reader = _reader_for(arguments[1], version)

        async def read_dict(ctx: ReadContext) -> dict[Any, Any]:
            result = {}
            for _ in range(await ctx.reader.read_uint64()):
                # Assignment evaluates its right-hand side before its target.
                # Keep the daemon's key-then-value wire order explicit.
                key = await key_reader(ctx)
                result[key] = await value_reader(ctx)
            return result

        return read_dict
    if isinstance(annotation, type) and issubclass(annotation, WireString):
        field_names = tuple(annotation.model_fields)
        single_field = field_names[0] if len(field_names) == 1 else None

        async def read_wire_string(ctx: ReadContext) -> WireString:
            # This is WireString.from_reader without its runtime _find_reader
            # lookup. Its compiled parent already owns the outermost failure
            # scope, so nesting a ContextVar scope here would only add cost.
            raw = await ctx.reader.read_string(str)
            if single_field is not None:
                obj = annotation.__new__(annotation)
                object.__setattr__(obj, "__pydantic_extra__", None)
                object.__setattr__(obj, "__pydantic_private__", None)
                object.__setattr__(obj, single_field, raw)
                object.__setattr__(obj, "__pydantic_fields_set__", {single_field})
                return obj

            data = annotation.from_str(raw)
            if not isinstance(data, dict):
                raise TypeError(f"from_str returned {type(data).__name__}, expected dict")
            obj = annotation.__new__(annotation)
            object.__setattr__(obj, "__pydantic_extra__", None)
            object.__setattr__(obj, "__pydantic_private__", None)
            object.__setattr__(obj, "__pydantic_fields_set__", set(data))
            for name, value in data.items():
                object.__setattr__(obj, name, value)
            return obj

        return read_wire_string
    if isinstance(annotation, type) and issubclass(annotation, WireModel):
        if _can_compile(annotation):
            codec = compile_codec(annotation, version)
            return codec.read

        async def read_custom_model(ctx: ReadContext) -> WireModel:
            return await annotation.from_reader(ctx)

        return read_custom_model
    raise TypeError(f"No reader for {annotation}")


@functools.cache
def compile_codec(model: type[WireModel], version: int) -> CompiledCodec:
    """Compile ``model`` for one protocol version using trusted local metadata.

    The emitted source is retained on the result to make the experiment
    inspectable in a REPL and in tests.  It is generated exclusively from
    package model fields; no protocol input is ever evaluated as Python code.
    """
    if not _can_compile(model):
        raise TypeError(f"{model.__name__} has a custom codec and cannot be compiled")

    fields = _wire_fields(model, version)
    write_predicates = tuple(
        predicate
        for _name, _annotation, predicate, serialize, _deserialize in fields
        if serialize and predicate is not None
    )
    read_predicates = tuple(
        predicate
        for _name, _annotation, predicate, _serialize, deserialize in fields
        if deserialize and predicate is not None
    )

    write_lines = ["async def write(value, ctx):"]
    if issubclass(model, WireRequest):
        write_lines.append("    await request_prelude(value, ctx)")
    write_emitter = _SourceEmitter(version)
    predicate_index = 0
    for name, annotation, predicate, serialize, _deserialize in fields:
        if not serialize:
            continue
        if predicate is None:
            write_emitter.write_value(write_lines, annotation, f"value.{name}", 4)
        else:
            write_lines.append(f"    if write_predicates[{predicate_index}](value):")
            write_emitter.write_value(write_lines, annotation, f"value.{name}", 8)
            predicate_index += 1

    read_lines = [
        "async def read(ctx):",
        "    with deserialization_scope(ctx, model):",
        "        obj = model.__new__(model)",
    ]
    for name, field in model.model_fields.items():
        if field.default is not PydanticUndefined:
            read_lines.append(f"        object.__setattr__(obj, {name!r}, defaults[{name!r}])")
        elif field.default_factory is not None:
            read_lines.append(f"        object.__setattr__(obj, {name!r}, factories[{name!r}]())")
    read_lines.extend(
        [
            "        object.__setattr__(obj, '__pydantic_fields_set__', set())",
            "        object.__setattr__(obj, '__pydantic_extra__', None)",
            "        object.__setattr__(obj, '__pydantic_private__', None)",
        ],
    )
    read_emitter = _SourceEmitter(version)
    predicate_index = 0
    for name, annotation, predicate, _serialize, deserialize in fields:
        if not deserialize:
            continue
        if predicate is None:
            local_name = f"_field_{name}"
            read_emitter.read_value(read_lines, annotation, local_name, 8)
            read_lines.append(f"        object.__setattr__(obj, {name!r}, {local_name})")
            read_lines.append(f"        obj.__pydantic_fields_set__.add({name!r})")
        else:
            local_name = f"_field_{name}"
            read_lines.append(f"        if read_predicates[{predicate_index}](obj):")
            read_emitter.read_value(read_lines, annotation, local_name, 12)
            read_lines.append(f"            object.__setattr__(obj, {name!r}, {local_name})")
            read_lines.append(f"            obj.__pydantic_fields_set__.add({name!r})")
            predicate_index += 1
    read_lines.append("        return obj")

    defaults = {
        name: field.default for name, field in model.model_fields.items() if field.default is not PydanticUndefined
    }
    factories = {
        name: field.default_factory for name, field in model.model_fields.items() if field.default_factory is not None
    }
    namespace: dict[str, Any] = {
        "deserialization_scope": deserialization_scope,
        "defaults": defaults,
        "factories": factories,
        "model": model,
        "object": object,
        "read_predicates": read_predicates,
        "read_adapters": tuple(read_emitter.adapters),
        "read_codecs": tuple(read_emitter.codecs),
        "read_enums": tuple(read_emitter.enums),
        "request_prelude": _write_request_prelude,
        "write_adapters": tuple(write_emitter.adapters),
        "write_codecs": tuple(write_emitter.codecs),
        "write_predicates": write_predicates,
    }
    write_source = "\n".join(write_lines)
    read_source = "\n".join(read_lines)
    write_source = write_source.replace("adapters[", "write_adapters[").replace("codecs[", "write_codecs[")
    read_source = (
        read_source.replace("adapters[", "read_adapters[")
        .replace("codecs[", "read_codecs[")
        .replace("enums[", "read_enums[")
    )
    exec(write_source, namespace)
    exec(read_source, namespace)
    return CompiledCodec(namespace["write"], namespace["read"], write_source, read_source)
