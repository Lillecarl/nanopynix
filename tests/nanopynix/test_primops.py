"""Tests for nanopynix.register_primop (Python functions as Nix builtins).

Primops are registered in conftest.py before the session EvalState is created.
"""


class TestRegisterPrimop:
    def test_unary(self, eval_state):
        v = eval_state.eval_string("test_add_one 41")
        assert v.as_int() == 42

    def test_binary(self, eval_state):
        v = eval_state.eval_string("test_add 40 2")
        assert v.as_int() == 42

    def test_string_arg_and_return(self, eval_state):
        v = eval_state.eval_string('test_shout "hello"')
        assert v.as_string() == "HELLO"

    def test_bool_arg(self, eval_state):
        v = eval_state.eval_string("test_not true")
        assert v.as_bool() is False
        v2 = eval_state.eval_string("test_not false")
        assert v2.as_bool() is True

    def test_list_arg(self, eval_state):
        v = eval_state.eval_string("test_sum [1 2 3 4]")
        assert v.as_int() == 10

    def test_attrs_arg(self, eval_state):
        v = eval_state.eval_string('test_get { x = 42; } "x"')
        assert v.as_int() == 42

    def test_return_none(self, eval_state):
        v = eval_state.eval_string("test_null 1")
        assert v.is_null()

    def test_return_float(self, eval_state):
        v = eval_state.eval_string("test_half 5")
        assert v.is_float()
        assert v.as_float() == 2.5

    def test_return_list(self, eval_state):
        v = eval_state.eval_string("test_range 3")
        assert v.list_length() == 3
        assert v.list_get(0).as_int() == 1
        assert v.list_get(2).as_int() == 3

    def test_return_dict(self, eval_state):
        v = eval_state.eval_string("test_make_attrs 10")
        assert v.is_attrs()
        assert v.attr_get("x").as_int() == 10
        assert v.attr_get("y").as_int() == 11

    def test_string_return(self, eval_state):
        v = eval_state.eval_string('test_greet "World"')
        assert v.as_string() == "Hello, World!"

    def test_nested_call(self, eval_state):
        v = eval_state.eval_string("test_double (test_double 5)")
        assert v.as_int() == 20

    def test_primop_in_function(self, eval_state):
        fn = eval_state.eval_string("x: test_triple (x + 1)")
        result = fn.call(eval_state.eval_string("13"))
        assert result.as_int() == 42

    def test_overwrite(self, eval_state):
        """Re-registering with same name uses the second definition."""
        v = eval_state.eval_string("test_overwrite 4")
        assert v.as_int() == 40

    def test_zero_arity(self, eval_state):
        v = eval_state.eval_string("test_answer")
        assert v.as_int() == 42

    def test_high_arity(self, eval_state):
        v = eval_state.eval_string("test_add4 10 10 10 12")
        assert v.as_int() == 42


class TestCallableToNixFunction:
    """Tests that Python callables returned from primops become Nix functions."""

    def test_return_callable_zero_arity(self, eval_state):
        """Zero-arg callable returned from a primop → evaluated immediately."""
        v = eval_state.eval_string("test_return_lazy_42")
        assert v.as_int() == 42

    def test_attrset_zero_arity_property(self, eval_state):
        """Attrset with a zero-arg callable → evaluated as a property value."""
        v = eval_state.eval_string("test_attrs_property 7")
        assert v.is_attrs()
        assert v.attr_get("result").as_int() == 49

    def test_attrset_one_arg_callable(self, eval_state):
        """Attrset with a 1-arg callable → becomes a Nix function. Call it from Nix."""
        v = eval_state.eval_string("(test_attrs_fn 10).add 11")
        assert v.as_int() == 21

    def test_attrset_two_arg_callable(self, eval_state):
        """Attrset with a 2-arg callable → becomes a Nix function. Call it twice."""
        v = eval_state.eval_string("(test_attrs_fn2 3).mul 2 5")
        assert v.as_int() == 30

    def test_closure_in_attrset(self, eval_state):
        """A callable in an attrset captures the primop's closure over its args."""
        v = eval_state.eval_string('(test_closure_fn 40 "hello").greet "world"')
        assert v.as_string() == "hello world 42"

    def test_callable_curry(self, eval_state):
        """A callable returned directly from a primop can be called from Nix."""
        v = eval_state.eval_string("(test_callable_curry 0 0) 21")
        assert v.as_int() == 42

    def test_value_from_python_lambda(self, eval_state):
        """value_from_python converts a lambda to a Nix primop value."""
        v = eval_state.value_from_python(lambda x: x * 3)
        result = v.call(eval_state.eval_string("5"))
        assert result.as_int() == 15

    def test_value_from_python_dict_with_lambda(self, eval_state):
        """value_from_python converts a dict with a lambda → attrset with Nix function."""
        v = eval_state.value_from_python({"add_one": lambda x: x + 1})
        assert v.is_attrs()
        fn = v.attr_get("add_one")
        result = fn.call(eval_state.eval_string("41"))
        assert result.as_int() == 42
