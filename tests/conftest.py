"""Shared fixtures for nanopynix tests."""

import atexit
import os
from typing import Protocol, cast

import pytest

import nanopynix


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-gc",
        action="store_true",
        default=False,
        help="run tests that perform destructive live Nix garbage collection",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_gc: test performs destructive live Nix garbage collection",
    )


def pytest_runtest_setup(item):
    if "live_gc" not in item.keywords:
        return
    if item.config.getoption("--run-live-gc") or os.environ.get("NANOPYNIX_RUN_LIVE_GC") == "1":
        return
    pytest.skip("destructive live GC test; pass --run-live-gc or NANOPYNIX_RUN_LIVE_GC=1 to run")


@pytest.fixture(scope="session", autouse=True)
def _init():
    """Initialize libstore, enable flakes (needed by fetchers/flake tests)."""
    nanopynix.init_libstore(load_config=False)


@pytest.fixture(scope="session")
def init_store():
    """Initialize libstore (once per session) without loading nix.conf."""
    nanopynix.init_libstore(load_config=False)


@pytest.fixture(scope="session")
def store(init_store):  # noqa: ARG001
    """Open the default Nix store (session-scoped)."""
    return nanopynix.open_store()


@pytest.fixture(scope="session")
def init_expr():
    """Initialize libexpr (once per session)."""
    nanopynix.init_libexpr()


@pytest.fixture(scope="session", autouse=True)
def _register_test_primops():
    """Register all test primops before EvalState is created."""
    nanopynix.register_primop("test_add_one", 1, ["x"], "increment by 1", lambda x: x + 1)
    nanopynix.register_primop("test_add", 2, ["x", "y"], "add two ints", lambda x, y: x + y)
    nanopynix.register_primop("test_shout", 1, ["s"], "uppercase a string", lambda s: s.upper())
    nanopynix.register_primop("test_not", 1, ["b"], "negate a boolean", lambda b: not b)
    nanopynix.register_primop("test_sum", 1, ["xs"], "sum a list of ints", lambda xs: sum(xs))
    nanopynix.register_primop("test_get", 2, ["attrs", "key"], "get attr from set", lambda attrs, key: attrs[key])
    nanopynix.register_primop("test_null", 1, ["_x"], "always returns null", lambda _x: None)
    nanopynix.register_primop("test_half", 1, ["x"], "divide by 2 as float", lambda x: x / 2.0)
    nanopynix.register_primop("test_range", 1, ["n"], "range(1, n+1)", lambda n: list(range(1, n + 1)))
    nanopynix.register_primop("test_make_attrs", 1, ["x"], "return { x = x; y = x+1; }", lambda x: {"x": x, "y": x + 1})
    nanopynix.register_primop("test_greet", 1, ["name"], "return greeting string", lambda name: f"Hello, {name}!")
    nanopynix.register_primop("test_double", 1, ["x"], "double an int", lambda x: x * 2)
    nanopynix.register_primop("test_triple", 1, ["x"], "triple an int", lambda x: x * 3)
    nanopynix.register_primop("test_overwrite", 1, ["x"], "first version", lambda x: x + 1)
    nanopynix.register_primop("test_overwrite", 1, ["x"], "second version — wins", lambda x: x * 10)
    nanopynix.register_primop("test_answer", 0, [], "the answer (zero arity)", lambda: 42)
    nanopynix.register_primop("test_add4", 4, ["a", "b", "c", "d"], "add 4 ints", lambda a, b, c, d: a + b + c + d)

    # Callable-returning primops (tests the Python-callable → Nix-function bridge).
    nanopynix.register_primop("test_return_lazy_42", 0, [], "returns a zero-arg lambda → 42", lambda: lambda: 42)
    nanopynix.register_primop(
        "test_attrs_property", 1, ["n"],
        "returns { result = lambda: n * n; } (zero-arg, evaluated immediately)",
        lambda n: {"result": lambda: n * n},
    )
    nanopynix.register_primop(
        "test_attrs_fn", 1, ["x"],
        "returns { add = lambda y: x + y; } (1-arg callable)",
        lambda x: {"add": lambda y: x + y},
    )
    nanopynix.register_primop(
        "test_attrs_fn2", 1, ["x"],
        "returns { mul = lambda a, b: x * a * b; } (2-arg callable)",
        lambda x: {"mul": lambda a, b: x * a * b},
    )
    nanopynix.register_primop(
        "test_closure_fn", 2, ["n", "prefix"],
        "returns { greet = lambda name: prefix + ' ' + name + ' ' + str(n+2); }",
        lambda n, prefix: {"greet": lambda name: f"{prefix} {name} {n + 2}"},
    )
    nanopynix.register_primop(
        "test_callable_curry", 2, ["a", "b"],
        "returns a callable that takes one arg",
        lambda _a, _b: lambda x: x * 2,
    )


@pytest.fixture(scope="session")
def eval_state(store, init_expr, _register_test_primops):  # noqa: ARG001
    """Create a session-scoped EvalState. Depends on _register_test_primops
    so that primops are registered before EvalState processes them."""
    return nanopynix.EvalState(store)


@pytest.fixture(scope="session", autouse=True)
def _enable_flakes():
    """Enable flakes experimental feature for all tests."""
    nanopynix.enable_experimental_feature("flakes")
    nanopynix.enable_experimental_feature("nix-command")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_primops():
    """Clear C++ primop registry at process exit to avoid segfault
    when nb::object destructors fire after Python finalization."""
    import nanopynix_expr

    class _PrimopRegistryModule(Protocol):
        def _cleanup_primop_registry(self) -> None: ...

    atexit.register(cast("_PrimopRegistryModule", nanopynix_expr)._cleanup_primop_registry)
