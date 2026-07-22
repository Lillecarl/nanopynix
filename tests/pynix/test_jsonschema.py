"""Tests for the generic, format-agnostic JSON Schema engine.

Pure schema-dict-in, plain-value-in -- no Nix evaluation, no LSP server,
no Terraform-specific knowledge. Kept independently fast per the "generic
engine reusable outside the LSP" design goal.
"""

from __future__ import annotations

from typing import Any

from pynix._jsonschema import FindingKind, list_properties, render, validate, walk

_RESOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "Content to store."},
        "id": {"type": "string", "readOnly": True},
        "file_permission": {"type": "string", "x-computed": True},
        "old_attr": {"type": "string", "deprecated": True},
        "filename": {"type": "string"},
        "triggers": {"type": "object", "additionalProperties": {"type": "string"}},
        "lifecycle": {
            "type": "object",
            "properties": {"prevent_destroy": {"type": "boolean"}},
            "additionalProperties": False,
        },
    },
    "required": ["filename"],
    "additionalProperties": False,
}


def test_validate_flags_an_unknown_property() -> None:
    findings = validate(_RESOURCE_SCHEMA, {"filename": "x", "content2": "oops"})
    assert any(f.kind is FindingKind.UNKNOWN_PROPERTY and f.path == ("content2",) for f in findings)


def test_validate_flags_an_unknown_nested_property() -> None:
    findings = validate(_RESOURCE_SCHEMA, {"filename": "x", "lifecycle": {"prevent_destroy2": True}})
    assert any(f.kind is FindingKind.UNKNOWN_PROPERTY and f.path == ("lifecycle", "prevent_destroy2") for f in findings)


def test_validate_flags_a_missing_required_property() -> None:
    findings = validate(_RESOURCE_SCHEMA, {"content": "x"})
    assert any(f.kind is FindingKind.MISSING_REQUIRED and f.path == ("filename",) for f in findings)


def test_validate_flags_a_type_mismatch() -> None:
    findings = validate(_RESOURCE_SCHEMA, {"filename": 4})
    assert any(f.kind is FindingKind.TYPE_MISMATCH and f.path == ("filename",) for f in findings)


def test_validate_accepts_a_conforming_value() -> None:
    assert validate(_RESOURCE_SCHEMA, {"filename": "x", "content": "y"}) == []


def test_validate_does_not_flag_a_map_shaped_additional_property() -> None:
    findings = validate(_RESOURCE_SCHEMA, {"filename": "x", "triggers": {"anything": "goes"}})
    assert findings == []


def test_walk_resolves_a_top_level_property() -> None:
    assert walk(_RESOURCE_SCHEMA, ("content",)) == {"type": "string", "description": "Content to store."}


def test_walk_resolves_a_nested_property() -> None:
    assert walk(_RESOURCE_SCHEMA, ("lifecycle", "prevent_destroy")) == {"type": "boolean"}


def test_walk_returns_none_for_an_unknown_property() -> None:
    assert walk(_RESOURCE_SCHEMA, ("nope",)) is None


def test_list_properties_lists_top_level_names() -> None:
    assert set(list_properties(_RESOURCE_SCHEMA, ()) or []) == set(_RESOURCE_SCHEMA["properties"])


def test_list_properties_lists_nested_names() -> None:
    assert list_properties(_RESOURCE_SCHEMA, ("lifecycle",)) == ["prevent_destroy"]


def test_render_includes_description_and_type() -> None:
    rendered = render({"type": "string", "description": "Content to store."})
    assert "Content to store." in rendered
    assert "string" in rendered


def test_render_includes_deprecated() -> None:
    assert "deprecated" in render({"type": "string", "deprecated": True})


def test_render_includes_read_only_for_computed_attributes() -> None:
    assert "computed" in render({"type": "string", "readOnly": True})


def test_render_includes_x_computed_annotation() -> None:
    assert "computed" in render({"type": "string", "x-computed": True})


def test_render_falls_back_when_nothing_to_say() -> None:
    assert "no schema description available" in render({})
