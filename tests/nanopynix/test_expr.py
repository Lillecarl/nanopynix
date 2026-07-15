"""Tests for nanopynix_expr (EvalState, Value, eval_string, eval_file, call)."""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import nanopynix
import pytest


class TestEvalString:
    def test_eval_int(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1 + 2")
        assert v.type() == "int"
        assert v.as_int() == 3

    def test_eval_float(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("3.14")
        assert v.type() == "float"
        assert v.as_float() == 3.14

    def test_eval_bool_true(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("true")
        assert v.type() == "bool"
        assert v.as_bool() is True

    def test_eval_bool_false(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("false")
        assert v.type() == "bool"
        assert v.as_bool() is False

    def test_eval_string(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('"hello"')
        assert v.type() == "string"
        assert v.as_string() == "hello"

    def test_eval_null(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("null")
        assert v.type() == "null"
        assert v.as_bool() is False  # null coerces to false

    def test_eval_nested_string_interpolation(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('"hello ${"world"}"')
        assert v.type() == "string"
        assert v.as_string() == "hello world"

    def test_value_keeps_eval_state_alive(self, store: Any, init_expr: object) -> None:
        eval_state = nanopynix.EvalState(store)
        value = eval_state.eval_string("42")

        del eval_state
        gc.collect()

        assert value.as_int() == 42


class TestEvalAttrs:
    def test_eval_simple_attrs(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('{ a = 1; b = true; c = "hi"; }')
        assert v.type() == "attrs"
        assert v.has_attr("a")
        assert v.has_attr("b")
        assert v.has_attr("c")
        attrs = v.attr_names()
        assert "a" in attrs
        assert "b" in attrs
        assert "c" in attrs

    def test_attr_get(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("{ x = 42; }")
        x = v.attr_get("x")
        assert x.as_int() == 42

    def test_attr_get_missing_raises(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("{ x = 1; }")
        with pytest.raises(RuntimeError, match="attribute 'y' not found"):
            v.attr_get("y")

    def test_attr_get_on_non_attrs_raises(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1")
        with pytest.raises(RuntimeError, match="not an attribute set"):
            v.attr_get("x")


class TestEvalList:
    def test_eval_list(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[1 2 3]")
        assert v.type() == "list"
        assert v.list_length() == 3

    def test_list_get(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[10 20 30]")
        assert v.list_get(0).as_int() == 10
        assert v.list_get(1).as_int() == 20
        assert v.list_get(2).as_int() == 30

    def test_list_get_out_of_range_raises(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[10 20 30]")
        with pytest.raises(IndexError, match="out of range"):
            v.list_get(3)

    def test_list_nested(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[[1 2] [3 4]]")
        assert v.list_length() == 2
        inner = v.list_get(0)
        assert inner.list_length() == 2


class TestToPython:
    def test_int(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("42")
        assert v.to_python() == 42

    def test_float(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("3.14")
        assert v.to_python() == 3.14

    def test_bool(self, eval_state: nanopynix.EvalState):
        assert eval_state.eval_string("true").to_python() is True
        assert eval_state.eval_string("false").to_python() is False

    def test_string(self, eval_state: nanopynix.EvalState):
        assert eval_state.eval_string('"hello"').to_python() == "hello"

    def test_null(self, eval_state: nanopynix.EvalState):
        assert eval_state.eval_string("null").to_python() is None

    def test_list(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('[1 true "hi" null]')
        assert v.to_python() == [1, True, "hi", None]

    def test_attrs_to_dict(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('{ a = 1; b = [true false]; c = "hi"; }')
        assert v.to_python() == {"a": 1, "b": [True, False], "c": "hi"}

    def test_nested_attrs(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("{ outer = { inner = 42; }; }")
        assert v.to_python() == {"outer": {"inner": 42}}


class TestCall:
    def test_call_function(self, eval_state: nanopynix.EvalState):
        fn = eval_state.eval_string("x: x + 1")
        assert fn.type() == "function"
        result = fn.call(eval_state.eval_string("41"))
        assert result.as_int() == 42

    def test_call_multi_arg(self, eval_state: nanopynix.EvalState):
        fn = eval_state.eval_string("x: y: x + y")
        mid = fn.call(eval_state.eval_string("10"))
        result = mid.call(eval_state.eval_string("32"))
        assert result.as_int() == 42

    def test_call_with_attrs(self, eval_state: nanopynix.EvalState):
        fn = eval_state.eval_string("{x, y}: x + y")
        arg = eval_state.eval_string("{ x = 40; y = 2; }")
        result = fn.call(arg)
        assert result.as_int() == 42


class TestValueTypes:
    def test_is_methods_int(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1")
        assert v.is_int()
        assert not v.is_float()
        assert not v.is_string()

    def test_is_methods_float(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1.5")
        assert v.is_float()
        assert not v.is_int()

    def test_is_methods_bool(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("true")
        assert v.is_bool()
        assert not v.is_int()

    def test_is_methods_string(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('"x"')
        assert v.is_string()
        assert not v.is_attrs()

    def test_is_methods_attrs(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("{}")
        assert v.is_attrs()

    def test_is_methods_list(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[]")
        assert v.is_list()

    def test_is_methods_function(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("x: x")
        assert v.is_function()

    def test_is_methods_null(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("null")
        assert v.is_null()


class TestForce:
    def test_force(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1 + 2")
        v.force()  # Should not raise

    def test_force_deep(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("rec { a = 1 + 2; b = [(3 + 4)]; }")
        v.force_deep()  # Should not raise


class TestEvalFile:
    def test_eval_file_simple(self, eval_state: nanopynix.EvalState, tmp_path: Path):
        nix_file = tmp_path / "test.nix"
        nix_file.write_text("42")
        result = nanopynix.eval_file(eval_state, str(nix_file))
        assert result.as_int() == 42


class TestAllocValue:
    def test_alloc_value(self, eval_state: nanopynix.EvalState):
        v = eval_state.alloc_value()
        assert v is not None
        # alloc'd values start as thunks/nulls — accessing type is fine
        assert isinstance(v.type(), str)
