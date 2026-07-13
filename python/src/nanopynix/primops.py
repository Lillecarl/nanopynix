"""Worker-side Python primop helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import TypeAdapter, ValidationError

from nanopynix.models import JsonValue, PrimOpSpec

if TYPE_CHECKING:
    from collections.abc import Iterable

_JsonValue = TypeAdapter(JsonValue)


def _yaml12_loader() -> type[Any]:
    class Loader(yaml.SafeLoader):  # type: ignore[reportUnknownBaseType]  # PyYAML stubs may be incomplete
        pass

    legacy_tags = {
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:int",
    }
    Loader.yaml_implicit_resolvers = {  # type: ignore[reportUnknownMemberType]  # yaml_implicit_resolvers class attribute not in stubs
        ch: [(tag, regexp) for tag, regexp in resolvers if tag not in legacy_tags]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()  # type: ignore[reportUnknownMemberType]  # yaml_implicit_resolvers not in stubs
    }

    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType]  # yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType]  # yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:int",
        re.compile(r"^[-+]?(?:[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
        list("-+0123456789"),
    )
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType]  # yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:float",
        re.compile(
            r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
            |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
            |[-+]?\.(?:inf|Inf|INF)
            |\.(?:nan|NaN|NAN))$""",
            re.VERBOSE,
        ),
        list("-+0123456789."),
    )
    Loader.add_constructor("tag:yaml.org,2002:int", _construct_yaml12_int)  # type: ignore[reportUnknownMemberType]  # yaml.Loader methods may not have complete stubs
    return Loader


def _construct_yaml12_int(loader: Any, node: Any) -> int:
    value = loader.construct_scalar(node).replace("_", "")
    sign = -1 if value.startswith("-") else 1
    unsigned = value[1:] if value.startswith(("-", "+")) else value
    if unsigned.startswith("0o"):
        return sign * int(unsigned, 0)
    if unsigned.startswith("0x"):
        return sign * int(unsigned, 0)
    return sign * int(unsigned, 10)


def _validate_document(value: Any, builtin: str) -> JsonValue:
    try:
        return _JsonValue.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"{builtin}: YAML document is not JSON-compatible: {exc}") from exc


def _validate_documents(values: Iterable[Any], builtin: str) -> list[JsonValue]:
    return [_validate_document(value, builtin) for value in values]


def _single_document(values: Iterable[Any], builtin: str, stream_builtin: str) -> JsonValue:
    docs = list(values)
    if len(docs) != 1:
        raise ValueError(
            f"{builtin}: expected exactly one YAML document, got {len(docs)}; "
            f"use {stream_builtin} for multi-document YAML"
        )
    return _validate_document(docs[0], builtin)


def _parse_error_message(exc: Exception) -> str:
    problem = getattr(exc, "problem", None)  # type: ignore[reportUnknownVariableType] -- dynamic attribute access on yaml exception
    mark = getattr(exc, "problem_mark", None)  # type: ignore[reportUnknownVariableType] -- dynamic attribute access on yaml exception
    if problem is None:
        return str(exc)
    if mark is None:
        return str(problem)
    return f"{problem} at line {mark.line + 1}, column {mark.column + 1}"  # type: ignore[reportUnknownMemberType]  # mark is Any from getattr on yaml exception


def from_yaml(source: str) -> JsonValue:
    """Parse YAML 1.2-style input into JSON-like Python values."""

    try:
        return _single_document(yaml.load_all(source, Loader=_yaml12_loader()), "fromYAML", "fromYAMLStream")
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML: failed to parse YAML 1.2 document: {_parse_error_message(exc)}") from exc


def from_yaml11(source: str) -> JsonValue:
    """Parse legacy YAML 1.1 input into JSON-like Python values."""

    try:
        return _single_document(yaml.safe_load_all(source), "fromYAML11", "fromYAML11Stream")
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML11: failed to parse YAML 1.1 document: {_parse_error_message(exc)}") from exc


def from_yaml_stream(source: str) -> list[JsonValue]:
    """Parse a YAML 1.2-style document stream into JSON-like Python values."""

    try:
        return _validate_documents(yaml.load_all(source, Loader=_yaml12_loader()), "fromYAMLStream")
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAMLStream: failed to parse YAML 1.2 stream: {_parse_error_message(exc)}") from exc


def from_yaml11_stream(source: str) -> list[JsonValue]:
    """Parse a legacy YAML 1.1 document stream into JSON-like Python values."""

    try:
        return _validate_documents(yaml.safe_load_all(source), "fromYAML11Stream")
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML11Stream: failed to parse YAML 1.1 stream: {_parse_error_message(exc)}") from exc


def to_yaml(value: JsonValue) -> str:
    """Render JSON-like Nix/Python values as Kubernetes-compatible YAML."""

    value = _validate_document(value, "toYAML")
    try:
        if isinstance(value, list):
            return yaml.safe_dump_all(value, explicit_start=True, sort_keys=False)
        return yaml.safe_dump(value, sort_keys=False)
    except yaml.YAMLError as exc:
        raise ValueError(f"toYAML: failed to render YAML: {_parse_error_message(exc)}") from exc


def yaml_primops() -> list[PrimOpSpec]:
    """Return worker primop specs for YAML parsing and rendering."""
    return [
        PrimOpSpec(
            name="fromYAML",
            arity=1,
            args=["source"],
            doc="Parse a YAML 1.2 string into a Nix value.",
            import_path="nanopynix.primops:from_yaml",
        ),
        PrimOpSpec(
            name="fromYAML11",
            arity=1,
            args=["source"],
            doc="Parse a YAML 1.1 string into a Nix value.",
            import_path="nanopynix.primops:from_yaml11",
        ),
        PrimOpSpec(
            name="fromYAMLStream",
            arity=1,
            args=["source"],
            doc="Parse a YAML 1.2 document stream into a Nix list.",
            import_path="nanopynix.primops:from_yaml_stream",
        ),
        PrimOpSpec(
            name="fromYAML11Stream",
            arity=1,
            args=["source"],
            doc="Parse a YAML 1.1 document stream into a Nix list.",
            import_path="nanopynix.primops:from_yaml11_stream",
        ),
        PrimOpSpec(
            name="toYAML",
            arity=1,
            args=["value"],
            doc="Render a Nix value as YAML; root lists render as document streams.",
            import_path="nanopynix.primops:to_yaml",
        ),
    ]
