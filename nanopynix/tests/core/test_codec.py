"""Tests for _codec.py -- the shared Nix ScalarValue/DeepValue <-> Python
value codec used by both the primop RPC backchannel and the eval RPC
transport. Pure Python, no C++/daemon dependency."""

from __future__ import annotations

from typing import Any

import pytest
from nanopynix_proto.nix.common import DeepAttrs, DeepList, DeepValue, NixType, NullValue, ScalarValue, ValueHandle

from nanopynix._core._codec import deep_value_to_python, python_to_deep_value, python_to_scalar, scalar_to_python


@pytest.mark.parametrize(
    ("scalar", "expected"),
    [
        (ScalarValue(string_value="hi"), "hi"),
        (ScalarValue(int_value=42), 42),
        (ScalarValue(float_value=1.5), 1.5),
        (ScalarValue(bool_value=True), True),
        (ScalarValue(null_value=NullValue()), None),
    ],
)
def test_scalar_to_python_covers_every_kind(scalar: ScalarValue, expected: Any) -> None:
    assert scalar_to_python(scalar) == expected


def test_scalar_to_python_none_input() -> None:
    assert scalar_to_python(None) is None


@pytest.mark.parametrize(
    ("value", "field", "expected"),
    [
        (None, "null_value", NullValue()),
        (True, "bool_value", True),
        (42, "int_value", 42),
        (1.5, "float_value", 1.5),
        ("hi", "string_value", "hi"),
    ],
)
def test_python_to_scalar_covers_every_type(value: Any, field: str, expected: Any) -> None:
    sv = python_to_scalar(value)
    assert getattr(sv, field) == expected


def test_python_to_scalar_rejects_unsupported_type_by_default() -> None:
    with pytest.raises(TypeError, match="unsupported RPC primop value type"):
        python_to_scalar(object())


def test_python_to_scalar_stringifies_unsupported_type_when_requested() -> None:
    """The eval RPC transport passes on_unsupported="stringify" since Nix
    path values arrive here as pathlib.Path, not a JSON scalar."""
    sv = python_to_scalar(object(), on_unsupported="stringify")
    assert sv.string_value is not None
    assert sv.string_value.startswith("<object")


def test_deep_value_scalar_roundtrip() -> None:
    dv = python_to_deep_value(42)
    assert dv.scalar is not None
    assert dv.scalar.int_value == 42
    assert deep_value_to_python(dv) == 42


def test_deep_value_list_roundtrip() -> None:
    dv = python_to_deep_value([1, "two", True, None])
    assert deep_value_to_python(dv) == [1, "two", True, None]


def test_deep_value_attrs_roundtrip() -> None:
    dv = python_to_deep_value({"a": 1, "b": {"c": [1, 2]}})
    assert deep_value_to_python(dv) == {"a": 1, "b": {"c": [1, 2]}}


def test_deep_value_to_python_none_input() -> None:
    assert deep_value_to_python(None) is None


def test_deep_value_to_python_rejects_remote_value() -> None:
    dv = DeepValue(remote_value=ValueHandle(handle=1, type=NixType.INT))
    with pytest.raises(TypeError, match="remote value handles are not supported"):
        deep_value_to_python(dv)


def test_python_to_deep_value_nested_dict_rejects_unsupported_leaf() -> None:
    with pytest.raises(TypeError, match="unsupported RPC primop value type"):
        python_to_deep_value({"a": object()})


def test_deep_value_empty_list_and_dict() -> None:
    assert deep_value_to_python(python_to_deep_value([])) == []
    assert deep_value_to_python(python_to_deep_value({})) == {}


def test_deep_attrs_and_list_constructed_directly() -> None:
    """Exercise decode via directly-constructed DeepAttrs/DeepList, not just
    round-tripping through python_to_deep_value."""
    dv = DeepValue(list=DeepList(items=[DeepValue(scalar=ScalarValue(int_value=1))]))
    assert deep_value_to_python(dv) == [1]

    dv = DeepValue(attrs=DeepAttrs(entries={"k": DeepValue(scalar=ScalarValue(string_value="v"))}))
    assert deep_value_to_python(dv) == {"k": "v"}
