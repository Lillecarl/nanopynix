"""Worker-side Python primop helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from nanopynix.models import JsonValue, PrimOpSpec

if TYPE_CHECKING:
    from collections.abc import Iterable

_JsonValue = TypeAdapter(JsonValue)


def _yaml() -> Any:
    import yaml

    return yaml


def _yaml12_loader() -> type[Any]:
    yaml = _yaml()

    class Loader(yaml.SafeLoader):
        pass

    legacy_tags = {
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:int",
    }
    Loader.yaml_implicit_resolvers = {
        ch: [(tag, regexp) for tag, regexp in resolvers if tag not in legacy_tags]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:int",
        re.compile(r"^[-+]?(?:[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
        list("-+0123456789"),
    )
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(
            r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
            |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
            |[-+]?\.(?:inf|Inf|INF)
            |\.(?:nan|NaN|NAN))$""",
            re.X,
        ),
        list("-+0123456789."),
    )
    Loader.add_constructor("tag:yaml.org,2002:int", _construct_yaml12_int)
    return Loader


def _construct_yaml12_int(loader: Any, node: Any) -> int:
    value = loader.construct_scalar(node).replace("_", "")
    sign = -1 if value.startswith("-") else 1
    unsigned = value[1:] if value.startswith(("-", "+")) else value
    if unsigned.startswith("0o"):
        return sign * int(unsigned[2:], 8)
    if unsigned.startswith("0x"):
        return sign * int(unsigned[2:], 16)
    return sign * int(unsigned, 10)


def _validate_document(value: Any) -> JsonValue:
    return _JsonValue.validate_python(value)


def _validate_documents(values: Iterable[Any]) -> list[JsonValue]:
    return [_validate_document(value) for value in values]


def _single_document(values: Iterable[Any]) -> JsonValue:
    docs = list(values)
    if len(docs) != 1:
        raise ValueError(f"expected exactly one YAML document, got {len(docs)}")
    return _validate_document(docs[0])


def from_yaml(source: str) -> JsonValue:
    """Parse YAML 1.2-style input into JSON-like Python values."""

    return _single_document(_yaml().load_all(source, Loader=_yaml12_loader()))


def from_yaml11(source: str) -> JsonValue:
    """Parse legacy YAML 1.1 input into JSON-like Python values."""

    return _single_document(_yaml().safe_load_all(source))


def from_yaml_stream(source: str) -> list[JsonValue]:
    """Parse a YAML 1.2-style document stream into JSON-like Python values."""

    return _validate_documents(_yaml().load_all(source, Loader=_yaml12_loader()))


def from_yaml11_stream(source: str) -> list[JsonValue]:
    """Parse a legacy YAML 1.1 document stream into JSON-like Python values."""

    return _validate_documents(_yaml().safe_load_all(source))


def to_yaml(value: JsonValue) -> str:
    """Render JSON-like Nix/Python values as Kubernetes-compatible YAML."""

    value = _JsonValue.validate_python(value)
    if isinstance(value, list):
        return _yaml().safe_dump_all(value, explicit_start=True, sort_keys=False)
    return _yaml().safe_dump(value, sort_keys=False)


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
