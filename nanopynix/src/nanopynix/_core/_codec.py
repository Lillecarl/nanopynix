"""Shared Nix ``ScalarValue``/``DeepValue`` <-> Python value codec.

Both the primop RPC backchannel (``rpc/worker/_worker_primop.py`` encoding
callback args/results, ``rpc/client/_manager.py`` decoding/encoding them on
the manager side) and the eval RPC transport (``rpc/client/_session.py``,
``rpc/worker/_worker_eval.py`` encoding individual scalar values) need to
convert between ``nanopynix_proto.nix.common`` wire types and Python values.
This used to be four independent copies of the same encoding; this module is
the single source of truth.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from nanopynix_proto.nix.common import DeepAttrs, DeepList, DeepValue, NullValue, ScalarValue


def scalar_to_python(sv: ScalarValue | None) -> Any:
    """Decode a proto ``ScalarValue`` to a Python scalar (str/int/float/bool/None).

    Checks each oneof field for ``is not None`` rather than using
    ``betterproto2.which_one_of`` -- behaviorally identical for a real proto
    message (an unset oneof field reads back as ``None``), but this also
    keeps working against the plain ``MagicMock`` stand-ins several unit
    tests use in place of a real ``ScalarValue``, which ``which_one_of``
    cannot introspect.
    """
    if sv is None:
        return None
    if sv.string_value is not None:
        return sv.string_value
    if sv.int_value is not None:
        return sv.int_value
    if sv.float_value is not None:
        return sv.float_value
    if sv.bool_value is not None:
        return sv.bool_value
    return None  # null_value or unset


def python_to_scalar(value: Any, *, on_unsupported: Literal["raise", "stringify"] = "raise") -> ScalarValue:
    """Encode a Python scalar to a proto ``ScalarValue``.

    ``on_unsupported`` controls what happens for values that are neither
    str/int/float/bool/None: ``"raise"`` for the primop RPC boundary (inputs
    there must already be JSON-compatible -- an unsupported type is a caller
    bug), ``"stringify"`` for the eval RPC transport (Nix values such as
    paths arrive here as e.g. ``pathlib.Path`` and must be coerced to a
    wire-representable string).
    """
    if value is None:
        return ScalarValue(null_value=NullValue())
    if isinstance(value, bool):
        return ScalarValue(bool_value=value)
    if isinstance(value, int):
        return ScalarValue(int_value=value)
    if isinstance(value, float):
        return ScalarValue(float_value=value)
    if isinstance(value, str):
        return ScalarValue(string_value=value)
    if on_unsupported == "stringify":
        return ScalarValue(string_value=str(value))
    raise TypeError(f"unsupported RPC primop value type: {type(value)}")


def python_to_deep_value(value: Any) -> DeepValue:
    """Encode a Python value (JSON-compatible: scalar/list/dict, arbitrarily
    nested) as the wire ``DeepValue`` primop RPC args/results carry."""
    if isinstance(value, dict):
        value_dict = cast("dict[str, Any]", value)
        return DeepValue(attrs=DeepAttrs(entries={k: python_to_deep_value(v) for k, v in value_dict.items()}))
    if isinstance(value, list):
        value_list = cast("list[Any]", value)
        return DeepValue(list=DeepList(items=[python_to_deep_value(v) for v in value_list]))
    return DeepValue(scalar=python_to_scalar(value))


def deep_value_to_python(dv: DeepValue | None) -> Any:
    """Decode a proto ``DeepValue`` (scalar/list/attrs, arbitrarily nested)
    back to a Python JSON-compatible value."""
    if dv is None:
        return None
    if dv.scalar is not None:
        return scalar_to_python(dv.scalar)
    if dv.list is not None:
        return [deep_value_to_python(item) for item in dv.list.items]
    if dv.attrs is not None:
        return {k: deep_value_to_python(v) for k, v in dv.attrs.entries.items()}
    if dv.remote_value is not None:
        raise TypeError("remote value handles are not supported for primop RPC args/results")
    return None
