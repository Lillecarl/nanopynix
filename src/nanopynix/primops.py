"""Worker-side Python primop helpers."""

from __future__ import annotations

from pydantic import TypeAdapter

from nanopynix.models import JsonValue, PrimOpSpec

_JsonValue = TypeAdapter(JsonValue)


def from_yaml(source: str) -> JsonValue:
    """Parse YAML into JSON-like Python values for conversion to Nix."""
    import yaml

    return _JsonValue.validate_python(yaml.safe_load(source))


def to_yaml(value: JsonValue) -> str:
    """Render JSON-like Nix/Python values as YAML."""
    import yaml

    value = _JsonValue.validate_python(value)
    return yaml.safe_dump(value, sort_keys=False)


def yaml_primops() -> list[PrimOpSpec]:
    """Return worker primop specs for ``fromYAML`` and ``toYAML``."""
    return [
        PrimOpSpec(
            name="fromYAML",
            arity=1,
            args=["source"],
            doc="Parse a YAML string into a Nix value.",
            import_path="nanopynix.primops:from_yaml",
        ),
        PrimOpSpec(
            name="toYAML",
            arity=1,
            args=["value"],
            doc="Render a Nix value as YAML.",
            import_path="nanopynix.primops:to_yaml",
        ),
    ]
