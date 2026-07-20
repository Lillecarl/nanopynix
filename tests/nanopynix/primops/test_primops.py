"""Tests for nanopynix.register_primop (Python functions as Nix builtins).

Primops are registered in conftest.py before the session EvalState is created.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import store as nanopynix_store

if TYPE_CHECKING:
    import nanopynix

pytestmark = pytest.mark.nix_version(minimum="2.32")


class TestRegisterPrimop:
    def test_unary(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_add_one 41")
        assert v.as_int() == 42

    def test_binary(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_add 40 2")
        assert v.as_int() == 42

    def test_string_arg_and_return(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('test_shout "hello"')
        assert v.as_string() == "HELLO"

    def test_bool_arg(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_not true")
        assert v.as_bool() is False
        v2 = eval_state.eval_string("test_not false")
        assert v2.as_bool() is True

    def test_list_arg(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_sum [1 2 3 4]")
        assert v.as_int() == 10

    def test_attrs_arg(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('test_get { x = 42; } "x"')
        assert v.as_int() == 42

    def test_return_none(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_null 1")
        assert v.is_null()

    def test_return_float(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_half 5")
        assert v.is_float()
        assert v.as_float() == 2.5

    def test_return_list(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_range 3")
        assert v.list_length() == 3
        assert v.list_get(0).as_int() == 1
        assert v.list_get(2).as_int() == 3

    def test_return_dict(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_make_attrs 10")
        assert v.is_attrs()
        assert v.attr_get("x").as_int() == 10
        assert v.attr_get("y").as_int() == 11

    def test_string_return(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('test_greet "World"')
        assert v.as_string() == "Hello, World!"

    def test_nested_call(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_double (test_double 5)")
        assert v.as_int() == 20

    def test_primop_in_function(self, eval_state: nanopynix.EvalState):
        fn = eval_state.eval_string("x: test_triple (x + 1)")
        result = fn.call(eval_state.eval_string("13"))
        assert result.as_int() == 42

    def test_overwrite(self, eval_state: nanopynix.EvalState):
        """Re-registering with same name uses the second definition."""
        v = eval_state.eval_string("test_overwrite 4")
        assert v.as_int() == 40

    def test_zero_arity(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_answer")
        assert v.as_int() == 42

    def test_high_arity(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("test_add4 10 10 10 12")
        assert v.as_int() == 42

    def test_string_arg_with_context_is_realised(self, eval_state: nanopynix.EvalState):
        """A string carrying string context (e.g. "${someDerivation}") must be
        built and substituted for the primop, not rejected -- primops run at
        eval time, not inside a derivation build, so callers would otherwise
        have to force realization themselves before calling in."""
        v = eval_state.eval_string("""
          let
            drv = derivation {
              name = "primop-string-context-test";
              system = builtins.currentSystem;
              builder = "/bin/sh";
              args = [ "-c" "echo hi > $out" ];
            };
          in test_identity_string "${drv}"
        """)
        result = v.as_string()
        assert result.endswith("primop-string-context-test")
        assert Path(result).exists()

    def test_string_return_value_carries_context_from_arg(
        self, eval_state: nanopynix.EvalState, store: Any
    ):
        """The string context of a context-bearing argument -- e.g.
        "${someDerivation}" -- must survive on the primop's *return* value
        too, not just get realised for the primop's own use.

        test_identity_string returns its argument unchanged, so if the
        returned value has no context, embedding it in a brand new
        derivation (as ordinary Nix string interpolation, not through a
        primop) won't record the original derivation as an input, and
        Nix's build-time reference scanner only checks a derivation's own
        *declared* inputs -- it doesn't scan the entire store for any
        substring match. A context-free result would build fine (the
        literal path text is still there) but silently drop the
        dependency from the built output's closure, which is exactly the
        kind of thing that breaks `nix copy`/GC-safety without ever
        raising an error.
        """
        v = eval_state.eval_string("""
          let
            leaf = derivation {
              name = "primop-context-propagation-leaf";
              system = builtins.currentSystem;
              builder = "/bin/sh";
              args = [ "-c" "echo hi > $out" ];
            };
          in derivation {
            name = "primop-context-propagation-consumer";
            system = builtins.currentSystem;
            builder = "/bin/sh";
            args = [ "-c" "echo ${test_identity_string "${leaf}"} > $out" ];
          }
        """)
        outputs = cast("dict[str, str]", v.build()["outputs"])
        out = outputs["out"]
        info = store.query_path_info(nanopynix_store.StorePath(Path(out).name))
        assert any("primop-context-propagation-leaf" in ref for ref in info["references"])


class TestCallableToNixFunction:
    """Tests that Python callables returned from primops become Nix functions."""

    def test_return_callable_zero_arity(self, eval_state: nanopynix.EvalState):
        """Zero-arg callable returned from a primop → evaluated immediately."""
        v = eval_state.eval_string("test_return_lazy_42")
        assert v.as_int() == 42

    def test_attrset_zero_arity_property(self, eval_state: nanopynix.EvalState):
        """Attrset with a zero-arg callable → evaluated as a property value."""
        v = eval_state.eval_string("test_attrs_property 7")
        assert v.is_attrs()
        assert v.attr_get("result").as_int() == 49

    def test_attrset_one_arg_callable(self, eval_state: nanopynix.EvalState):
        """Attrset with a 1-arg callable → becomes a Nix function. Call it from Nix."""
        v = eval_state.eval_string("(test_attrs_fn 10).add 11")
        assert v.as_int() == 21

    def test_attrset_two_arg_callable(self, eval_state: nanopynix.EvalState):
        """Attrset with a 2-arg callable → becomes a Nix function. Call it twice."""
        v = eval_state.eval_string("(test_attrs_fn2 3).mul 2 5")
        assert v.as_int() == 30

    def test_closure_in_attrset(self, eval_state: nanopynix.EvalState):
        """A callable in an attrset captures the primop's closure over its args."""
        v = eval_state.eval_string('(test_closure_fn 40 "hello").greet "world"')
        assert v.as_string() == "hello world 42"

    def test_callable_curry(self, eval_state: nanopynix.EvalState):
        """A callable returned directly from a primop can be called from Nix."""
        v = eval_state.eval_string("(test_callable_curry 0 0) 21")
        assert v.as_int() == 42

    def test_value_from_python_lambda(self, eval_state: nanopynix.EvalState) -> None:
        """value_from_python converts a lambda to a Nix primop value."""
        def triple(value: int) -> int:
            return value * 3

        v = eval_state.value_from_python(triple)
        result = v.call(eval_state.eval_string("5"))
        assert result.as_int() == 15

    def test_value_from_python_dict_with_lambda(self, eval_state: nanopynix.EvalState) -> None:
        """value_from_python converts a dict with a lambda → attrset with Nix function."""
        def add_one(value: int) -> int:
            return value + 1

        v = eval_state.value_from_python({"add_one": add_one})
        assert v.is_attrs()
        fn = v.attr_get("add_one")
        result = fn.call(eval_state.eval_string("41"))
        assert result.as_int() == 42
