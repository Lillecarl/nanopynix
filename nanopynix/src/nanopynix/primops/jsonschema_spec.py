"""Primop specs for JSON Schema validation."""

from __future__ import annotations

from nanopynix.models import PrimOpSpec as PrimOpSpec


def jsonschema_primops() -> list[PrimOpSpec]:
    return [
        PrimOpSpec(
            name="validateJSONSchema",
            arity=2,
            args=["value", "schemaPath"],
            doc=(
                "Validate a Nix value against a JSON Schema file. ``schemaPath``\n"
                "must be a Nix path -- e.g. a local file or a fetcher's output --\n"
                "so schemas stay store-tracked inputs rather than being fetched at\n"
                "evaluation time. Returns ``value`` unchanged on success; raises\n"
                "listing every violation on failure."
            ),
            import_path="nanopynix.primops._jsonschema_impl:validate_json_schema",
        ),
    ]
