"""Generic, format-agnostic JSON Schema validation/introspection engine.

Pure JSON-Schema-dict-in, plain-Python-value-in logic. No dependency on
lsprotocol, tree-sitter, or nanopynix -- this must stay usable standalone
(e.g. a future easykubenix Kubernetes dialect, or a plain "point this at any
.schema.json file" generic dialect) with zero LSP-specific concepts leaking
in. Format-specific code (``pynix._lsp._terranix_jsonschema``, and later a
Kubernetes equivalent) is responsible for converting its own native schema
representation into a plain dict shaped like this module expects, and for
mapping this module's generic :class:`Finding`\\ s to that format's own
rule-code namespace (``TF001``, later ``K8S001``, ...).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

import jsonschema
import jsonschema.validators


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


class FindingKind(Enum):
    """Small, closed, format-agnostic set of validation-failure categories.

    Format-specific modules (Terraform now, Kubernetes later) map these to
    their OWN rule-code namespace (``TF001``, later ``K8S001``), so this
    engine never needs to know either one's vocabulary.
    """

    UNKNOWN_PROPERTY = "unknown-property"
    MISSING_REQUIRED = "missing-required"
    TYPE_MISMATCH = "type-mismatch"
    OTHER = "other"


@dataclass(frozen=True)
class Finding:
    path: tuple[str | int, ...]
    kind: FindingKind
    message: str
    severity: Severity = Severity.WARNING


@dataclass(frozen=True)
class _RawError:
    """The handful of ``jsonschema.ValidationError`` fields this module needs, typed once at the boundary.

    ``jsonschema`` ships no ``py.typed`` marker, so every attribute access on
    a raw ``ValidationError`` resolves to ``Unknown`` under this repo's
    strict pyright config. Extracting them here, once, via explicit
    ``cast``s (not a blanket suppression) means every other function in this
    module works against ordinary, fully-typed values.
    """

    validator: str | None
    absolute_path: tuple[str | int, ...]
    instance: Any
    schema: Any
    validator_value: Any
    message: str


def _extract(error: jsonschema.ValidationError) -> _RawError:
    return _RawError(
        validator=cast("str | None", error.validator),
        absolute_path=tuple(cast("list[str | int]", error.absolute_path)),
        instance=cast("Any", error.instance),
        schema=cast("Any", error.schema),
        validator_value=cast("Any", error.validator_value),
        message=error.message,
    )


def _as_str_keyed_dict(value: Any) -> dict[str, Any]:
    # isinstance(value, dict) narrows to the bare, generic-less `dict[Unknown,
    # Unknown]` (isinstance can't check generic parameters at runtime, and
    # pyright reflects that) -- re-declaring the checked value's type here via
    # cast (not a suppression -- a normal, honest type assertion once the
    # isinstance check has actually verified it's a dict at runtime) is what
    # stops that Unknown from then propagating into every caller that
    # indexes/iterates the result.
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    # Same isinstance-narrowing-loses-generics issue as _as_str_keyed_dict, for lists.
    return cast("list[Any]", value) if isinstance(value, list) else []


def _unknown_property_findings(error: _RawError) -> list[Finding]:
    # jsonschema reports every extra key in ONE "additionalProperties" error's
    # message rather than one error per key, so the specific offending keys
    # have to be re-derived here (instance keys minus the schema's declared
    # properties) to anchor a separate Finding -- and separate diagnostic --
    # per unknown key.
    instance = _as_str_keyed_dict(error.instance)
    declared = _as_str_keyed_dict(_as_str_keyed_dict(error.schema).get("properties", {}))
    extra_keys = sorted(set(instance) - set(declared))
    return [
        Finding(
            path=(*error.absolute_path, key),
            kind=FindingKind.UNKNOWN_PROPERTY,
            message=f"{key!r} is not a known property",
        )
        for key in extra_keys
    ]


def _missing_required_findings(error: _RawError) -> list[Finding]:
    # Same story as additionalProperties: one error lists every missing
    # required property together, so the individual names are re-derived
    # from the schema's own "required" list against what's actually present.
    instance = _as_str_keyed_dict(error.instance)
    required_names = _as_list(error.validator_value)
    missing_keys = [key for key in required_names if isinstance(key, str) and key not in instance]
    return [
        Finding(
            path=(*error.absolute_path, key),
            kind=FindingKind.MISSING_REQUIRED,
            message=f"{key!r} is a required property",
        )
        for key in missing_keys
    ]


def validate(schema: dict[str, Any], value: object) -> list[Finding]:
    """Validate *value* against *schema*, returning every violation as a :class:`Finding`.

    Uses ``jsonschema.validators.validator_for(schema)`` (auto-detects draft
    from the schema's own ``$schema`` keyword, falling back to the latest
    draft when absent -- the same policy already established by
    ``nanopynix.primops._jsonschema_impl``) rather than pinning a draft
    unconditionally, so a caller that hands in a schema with an explicit
    ``$schema`` (e.g. a future generic "point this at any .schema.json"
    dialect) is honored. Terraform-derived schemas never set ``$schema``, so
    this resolves to the same ``Draft202012Validator`` either way.
    """
    # jsonschema ships no py.typed marker, so validator_for's return type
    # (and the resulting validator instance) resolve to Unknown.
    validator_cls = cast("Any", jsonschema.validators.validator_for)(schema)  # type: ignore[reportUnknownMemberType] -- jsonschema ships no py.typed marker
    validator = validator_cls(schema)
    findings: list[Finding] = []
    # jsonschema ships no py.typed marker, so pyright can't verify
    # iter_errors' overloads even though the library's own source has inline
    # type hints -- this is a real external-library limitation, not
    # something a cast on our own argument can resolve.
    iter_errors = cast("Any", validator.iter_errors)  # type: ignore[reportUnknownMemberType] -- jsonschema ships no py.typed marker
    for raw_error in iter_errors(value):
        error = _extract(raw_error)
        if error.validator == "additionalProperties":
            findings.extend(_unknown_property_findings(error))
        elif error.validator == "required":
            findings.extend(_missing_required_findings(error))
        elif error.validator == "type":
            findings.append(Finding(path=error.absolute_path, kind=FindingKind.TYPE_MISMATCH, message=error.message))
        else:
            findings.append(Finding(path=error.absolute_path, kind=FindingKind.OTHER, message=error.message))
    return findings


def _deref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a Kubernetes-shaped ``{"$ref": "#/definitions/<key>"}`` fragment against *root*, one hop.

    Kubernetes' own OpenAPI schema refs are always exactly this shape (a
    flat key directly under ``#/definitions/``) -- this is not a general
    RFC 6901 JSON Pointer resolver. terranix's adapter always fully inlines
    its converted schemas (no ``$ref`` ever appears), so this is a no-op for
    every existing caller that never passes a ``$ref``-bearing fragment.
    """
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/definitions/"):
        return schema
    definitions = _as_str_keyed_dict(root.get("definitions"))
    target = definitions.get(ref.removeprefix("#/definitions/"))
    return _as_str_keyed_dict(target) if isinstance(target, dict) else schema


def walk(
    schema: dict[str, Any], path: tuple[str, ...], *, root: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return the schema fragment at *path*, descending one ``properties``/``additionalProperties`` hop per segment.

    *root* is the document ``$ref``s resolve against, defaulting to *schema*
    itself. Pass the full OpenAPI document (with its own ``definitions``)
    for a ``$ref``-heavy schema; irrelevant, and safely omittable, for an
    already-fully-inlined one like terranix's.
    """
    root = schema if root is None else root
    current = _deref(schema, root)
    for segment in path:
        properties = _as_str_keyed_dict(current.get("properties"))
        if segment in properties:
            current = _deref(_as_str_keyed_dict(properties[segment]), root)
            continue
        additional = current.get("additionalProperties")
        if isinstance(additional, dict):
            current = _deref(_as_str_keyed_dict(additional), root)
            continue
        return None
    return current


def list_properties(
    schema: dict[str, Any], path: tuple[str, ...], *, root: dict[str, Any] | None = None
) -> list[str] | None:
    """List declared property names at *path* within *schema* (for completion)."""
    root = schema if root is None else root
    fragment = walk(schema, path, root=root) if path else _deref(schema, root)
    if fragment is None:
        return None
    properties = fragment.get("properties")
    if not isinstance(properties, dict):
        return None
    return list(_as_str_keyed_dict(properties))


def _describe_type(schema: dict[str, Any], root: dict[str, Any]) -> str | None:
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        item_type = _describe_type(_deref(_as_str_keyed_dict(items), root), root) if isinstance(items, dict) else None
        return f"array of {item_type}" if item_type else "array"
    if isinstance(schema_type, str):
        return schema_type
    return None


def render(schema: dict[str, Any], *, root: dict[str, Any] | None = None) -> str:
    """Render one schema fragment's description/type/deprecated/computed annotations as hover Markdown."""
    root = schema if root is None else root
    schema = _deref(schema, root)
    sections: list[str] = []
    description = schema.get("description")
    if description:
        sections.append(str(description))
    schema_type = _describe_type(schema, root)
    if schema_type is not None:
        sections.append(f"type: `{schema_type}`")
    if schema.get("readOnly"):
        sections.append("computed (read-only)")
    elif schema.get("x-computed"):
        sections.append("optional -- may be left to be computed")
    if schema.get("deprecated"):
        sections.append("deprecated")
    return "\n\n".join(sections) if sections else "```\nno schema description available\n```"
