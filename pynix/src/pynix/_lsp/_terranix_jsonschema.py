"""Converts already-parsed Terraform/OpenTofu schema models into plain JSON Schema.

The only Terraform-specific code in the schema-diagnostics feature --
everything else (``pynix._jsonschema``) is format-agnostic and reusable for
a future Kubernetes-via-OpenAPI dialect or a plain "point this at any
.schema.json" generic dialect. This module's job is narrow: take the
``SchemaBlock``/``SchemaAttribute`` models ``_terranix_schema.py`` already
parses from ``tofu providers schema -json`` (or the core-schema tool's
equivalent shape) and produce a dict shaped like an ordinary JSON Schema
object, so ``pynix._jsonschema.validate``/``walk``/``render`` can operate on
it with zero knowledge of Terraform.

cty type-constraint JSON (Terraform's ``type`` field) is not JSON Schema
syntax and is translated here: ``"string"``/``"number"``/``"bool"``
(primitives, 1:1), ``["list"|"set", T]`` (array+items), ``["map", T]``
(object+additionalProperties), ``["object", {...}]``
(properties+additionalProperties:false), ``["tuple", [...]]``
(prefixItems), ``"dynamic"``/anything unrecognized (unconstrained ``{}``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pynix._lsp._terranix_schema import SchemaAttribute, SchemaBlock


def cty_type_to_json_schema(cty_type: Any) -> dict[str, Any]:
    """Translate one cty type-constraint JSON value into a JSON Schema fragment."""
    if cty_type == "string":
        return {"type": "string"}
    if cty_type == "number":
        return {"type": "number"}
    if cty_type == "bool":
        return {"type": "boolean"}
    if isinstance(cty_type, list) and len(cast("list[Any]", cty_type)) == 2:
        kind, arg = cast("tuple[Any, Any]", cty_type)
        if kind in ("list", "set"):
            return {"type": "array", "items": cty_type_to_json_schema(arg)}
        if kind == "map":
            return {"type": "object", "additionalProperties": cty_type_to_json_schema(arg)}
        if kind == "object" and isinstance(arg, dict):
            arg_dict = cast("dict[str, Any]", arg)
            properties = {name: cty_type_to_json_schema(member_type) for name, member_type in arg_dict.items()}
            return {"type": "object", "properties": properties, "additionalProperties": False}
        if kind == "tuple" and isinstance(arg, list):
            arg_list = cast("list[Any]", arg)
            return {"type": "array", "prefixItems": [cty_type_to_json_schema(member_type) for member_type in arg_list]}
    # "dynamic" or any other unrecognized shape: unconstrained, matching
    # cty's own "any" semantics rather than under- or over-flagging.
    return {}


def attribute_to_json_schema(attribute: SchemaAttribute) -> dict[str, Any]:
    """Convert one ``SchemaAttribute`` into a JSON Schema fragment, preserving every piece of metadata."""
    fragment = cty_type_to_json_schema(attribute.type)
    if attribute.description:
        fragment["description"] = attribute.description
    if attribute.deprecated:
        fragment["deprecated"] = True
    if attribute.computed and not attribute.optional:
        fragment["readOnly"] = True
    elif attribute.computed and attribute.optional:
        # No native JSON Schema keyword covers "computed AND optional" (e.g.
        # local_file's file_permission): a small vendor extension keyword,
        # same idiom as OpenAPI's own "x-*" extensions -- unknown keywords
        # are simply ignored by validators, so this is additive, not risky.
        fragment["x-computed"] = True
    return fragment


def block_to_json_schema(block: SchemaBlock) -> dict[str, Any]:
    """Convert one ``SchemaBlock`` (a resource/data-source or nested block) into a JSON Schema object.

    Known gap: Terraform's "nested attribute type" mechanism (``nested_type``
    on an attribute, distinct from ``block_types``) isn't modeled by
    ``SchemaAttribute`` yet, so such attributes validate as unconstrained
    (``{}``, via ``cty_type_to_json_schema``'s fallback) rather than
    under- or over-flagging.
    """
    properties: dict[str, Any] = {name: attribute_to_json_schema(attr) for name, attr in block.attributes.items()}
    required = sorted(name for name, attr in block.attributes.items() if attr.required)
    for name, nested in block.block_types.items():
        nested_schema = block_to_json_schema(nested.block)
        if nested.nesting_mode in ("list", "set"):
            properties[name] = {"type": "array", "items": nested_schema}
        else:
            properties[name] = nested_schema
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    if block.description:
        schema["description"] = block.description
    if block.deprecated:
        schema["deprecated"] = True
    return schema
