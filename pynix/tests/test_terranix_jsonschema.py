"""Tests for the Terraform-schema -> JSON Schema adapter.

Hand-built ``SchemaBlock``/``SchemaAttribute`` instances, no Nix build and no
LSP server involved -- ``pynix._jsonschema`` itself is already covered by
``test_jsonschema.py``, so these tests only need to prove the *conversion*
is correct, then confirm the result actually behaves under
``pynix._jsonschema.validate``.
"""

from __future__ import annotations

from pynix._jsonschema import FindingKind, validate
from pynix._lsp._terranix_jsonschema import attribute_to_json_schema, block_to_json_schema, cty_type_to_json_schema
from pynix._lsp._terranix_schema import SchemaAttribute, SchemaBlock, SchemaNestedBlock


def test_cty_type_translates_primitives() -> None:
    assert cty_type_to_json_schema("string") == {"type": "string"}
    assert cty_type_to_json_schema("number") == {"type": "number"}
    assert cty_type_to_json_schema("bool") == {"type": "boolean"}


def test_cty_type_translates_list_and_set() -> None:
    assert cty_type_to_json_schema(["list", "string"]) == {"type": "array", "items": {"type": "string"}}
    assert cty_type_to_json_schema(["set", "string"]) == {"type": "array", "items": {"type": "string"}}


def test_cty_type_translates_map() -> None:
    assert cty_type_to_json_schema(["map", "string"]) == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }


def test_cty_type_translates_object() -> None:
    result = cty_type_to_json_schema(["object", {"name": "string", "size": "number"}])
    assert result == {
        "type": "object",
        "properties": {"name": {"type": "string"}, "size": {"type": "number"}},
        "additionalProperties": False,
    }


def test_cty_type_translates_tuple() -> None:
    result = cty_type_to_json_schema(["tuple", ["string", "number"]])
    assert result == {"type": "array", "prefixItems": [{"type": "string"}, {"type": "number"}]}


def test_cty_type_translates_dynamic_as_unconstrained() -> None:
    assert cty_type_to_json_schema("dynamic") == {}


def test_attribute_marks_pure_computed_as_read_only() -> None:
    attribute = SchemaAttribute(type="string", computed=True, optional=False)
    assert attribute_to_json_schema(attribute) == {"type": "string", "readOnly": True}


def test_attribute_marks_computed_and_optional_with_vendor_extension() -> None:
    attribute = SchemaAttribute(type="string", computed=True, optional=True)
    assert attribute_to_json_schema(attribute) == {"type": "string", "x-computed": True}


def test_attribute_carries_description_and_deprecated() -> None:
    attribute = SchemaAttribute(type="string", description="Content to store.", deprecated=True)
    fragment = attribute_to_json_schema(attribute)
    assert fragment["description"] == "Content to store."
    assert fragment["deprecated"] is True


def test_block_hoists_required_attributes() -> None:
    block = SchemaBlock(
        attributes={
            "filename": SchemaAttribute(type="string", required=True),
            "content": SchemaAttribute(type="string", optional=True),
        },
    )
    schema = block_to_json_schema(block)
    assert schema["required"] == ["filename"]
    assert set(schema["properties"]) == {"filename", "content"}
    assert schema["additionalProperties"] is False


def test_block_converts_nested_single_block_as_object() -> None:
    block = SchemaBlock(
        block_types={
            "lifecycle": SchemaNestedBlock(
                block=SchemaBlock(attributes={"prevent_destroy": SchemaAttribute(type="bool", optional=True)}),
                nesting_mode="single",
            ),
        },
    )
    schema = block_to_json_schema(block)
    assert schema["properties"]["lifecycle"] == {
        "type": "object",
        "properties": {"prevent_destroy": {"type": "boolean"}},
        "additionalProperties": False,
    }


def test_block_converts_nested_list_block_as_array_of_object() -> None:
    block = SchemaBlock(
        block_types={
            "ingress": SchemaNestedBlock(
                block=SchemaBlock(attributes={"port": SchemaAttribute(type="number", required=True)}),
                nesting_mode="list",
            ),
        },
    )
    schema = block_to_json_schema(block)
    assert schema["properties"]["ingress"]["type"] == "array"
    assert schema["properties"]["ingress"]["items"]["properties"]["port"] == {"type": "number"}
    assert schema["properties"]["ingress"]["items"]["required"] == ["port"]


def test_block_carries_its_own_description_and_deprecated() -> None:
    """Block-level ``description``/``deprecated`` (rare in practice -- see _terranix_schema.py's SchemaBlock) survive the conversion."""
    block = SchemaBlock(description="A local file resource.", deprecated=True)
    schema = block_to_json_schema(block)
    assert schema["description"] == "A local file resource."
    assert schema["deprecated"] is True


def test_block_without_a_description_omits_the_key() -> None:
    block = SchemaBlock(attributes={"filename": SchemaAttribute(type="string")})
    schema = block_to_json_schema(block)
    assert "description" not in schema
    assert "deprecated" not in schema


def test_converted_block_validates_via_the_generic_engine() -> None:
    block = SchemaBlock(
        attributes={
            "filename": SchemaAttribute(type="string", required=True),
            "content": SchemaAttribute(type="string", optional=True),
        },
    )
    schema = block_to_json_schema(block)
    findings = validate(schema, {"filename": "x", "content2": "oops"})
    assert any(f.kind is FindingKind.UNKNOWN_PROPERTY and f.path == ("content2",) for f in findings)
