"""JSON Schema validation primop implementation — worker-side.

``schemaPath`` is expected to be a Nix path, not a string: Nix coerces path
arguments to an absolute filesystem path before this function ever sees them,
so schemas are read from wherever Nix put them (a local file copied into the
store as part of evaluation, or the output of a fetcher). This primop never
does network I/O itself -- pulling remote schemas is a job for a Nix fetcher,
keeping evaluation pure.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema
import jsonschema.exceptions
import jsonschema.validators
import pydantic_core
from nanopynix_bindings.expr import PrimopError

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterable

    from nanopynix.models import JsonValue


def _read_schema(schema_path: str) -> Any:
    try:
        text = Path(schema_path).read_text()
    except OSError as exc:
        raise PrimopError(f"validateJSONSchema: failed to read schema file '{schema_path}': {exc}") from exc
    try:
        return pydantic_core.from_json(text)
    except ValueError as exc:
        raise PrimopError(f"validateJSONSchema: schema file '{schema_path}' is not valid JSON: {exc}") from exc


def _format_error(error: jsonschema.exceptions.ValidationError) -> str:
    location = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"


def _format_errors(errors: Iterable[jsonschema.exceptions.ValidationError]) -> str:
    return "\n".join(f"  - {_format_error(error)}" for error in errors)


def validate_json_schema(value: JsonValue, schema_path: str) -> JsonValue:
    """Validate a Nix value against a JSON Schema file read from the Nix store.

    The validator is picked from the schema's own ``$schema`` field (falling
    back to the latest draft the ``jsonschema`` package knows if unset), so
    any draft it supports works without nanopynix hardcoding one. Returns
    ``value`` unchanged on success so this can be used inline; raises with
    every violation listed (not just the first) on failure.
    """
    schema = _read_schema(schema_path)
    validator_cls = jsonschema.validators.validator_for(  # type: ignore[reportUnknownMemberType] -- jsonschema's bundled stubs leave the schema param under-specified
        schema,
    )
    try:
        validator_cls.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        raise PrimopError(f"validateJSONSchema: schema file '{schema_path}' is invalid: {exc.message}") from exc

    validator = validator_cls(schema)
    errors = sorted(validator.iter_errors(value), key=lambda error: list(map(str, error.absolute_path)))
    if errors:
        raise PrimopError(
            f"validateJSONSchema: value does not match schema '{schema_path}':\n{_format_errors(errors)}",
        )
    return value
