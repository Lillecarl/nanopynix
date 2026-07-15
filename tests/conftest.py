"""Shared fixtures for nanopynix tests."""

import atexit
import os
import re
from typing import Any, Protocol, cast

import pytest

import nanopynix
import nanopynix_expr


def pytest_addoption(parser: pytest.Parser):
    parser.addoption(
        "--run-temp-store-builds",
        action="store_true",
        default=False,
        help="run tests that build into temporary Nix stores",
    )
    parser.addoption(
        "--run-live-gc",
        action="store_true",
        default=False,
        help="run tests that perform destructive live Nix garbage collection",
    )


def pytest_configure(config: pytest.Config):
    config.addinivalue_line(
        "markers",
        "live_gc: test performs destructive live Nix garbage collection",
    )
    config.addinivalue_line(
        "markers",
        "required_nix_version(min, max): require linked Nix in [min, max); use None for no bound",
    )


def _nix_version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"(\d+(?:\.\d+)*)", value)
    if match is None:
        raise ValueError(f"cannot parse linked Nix version: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def _version_at_least(actual: tuple[int, ...], required: tuple[int, ...]) -> bool:
    width = max(len(actual), len(required))
    return actual + (0,) * (width - len(actual)) >= required + (0,) * (width - len(required))


def pytest_collection_modifyitems(_config: pytest.Config, items: list[pytest.Item]) -> None:
    actual = _nix_version_tuple(nanopynix.build_info()["nix_version"])
    for item in items:
        marker = item.get_closest_marker("required_nix_version")
        if marker is None:
            continue
        if marker.kwargs or len(marker.args) != 2:
            raise pytest.UsageError(
                "required_nix_version requires exactly two positional arguments: min, max"
            )

        minimum, maximum = marker.args
        if minimum is not None and not isinstance(minimum, str):
            raise pytest.UsageError("required_nix_version min must be a string or None")
        if maximum is not None and not isinstance(maximum, str):
            raise pytest.UsageError("required_nix_version max must be a string or None")

        if minimum is not None and not _version_at_least(actual, _nix_version_tuple(minimum)):
            item.add_marker(pytest.mark.skip(reason=f"requires Nix >= {minimum}"))
        if maximum is not None and _version_at_least(actual, _nix_version_tuple(maximum)):
            item.add_marker(pytest.mark.skip(reason=f"requires Nix < {maximum}"))


def pytest_runtest_setup(item: pytest.Item):
    if "live_gc" not in item.keywords:
        return
    if item.config.getoption("--run-live-gc") or os.environ.get("NANOPYNIX_RUN_LIVE_GC") == "1":
        return
    pytest.skip("destructive live GC test; pass --run-live-gc or NANOPYNIX_RUN_LIVE_GC=1 to run")


@pytest.fixture(scope="session", autouse=True)
def _init():  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Initialize libstore, enable flakes (needed by fetchers/flake tests)."""
    nanopynix.init_libstore(load_config=False)


@pytest.fixture(scope="session")
def init_store() -> None:
    """Initialize libstore (once per session) without loading nix.conf."""
    nanopynix.init_libstore(load_config=False)


@pytest.fixture(scope="session")
def store(init_store: object) -> object:  # noqa: ARG001
    """Open the default Nix store (session-scoped)."""
    return nanopynix.open_store()


@pytest.fixture(scope="session")
def init_expr() -> None:
    """Initialize libexpr (once per session)."""
    nanopynix.init_libexpr()


@pytest.fixture(scope="session", autouse=True)
def _register_test_primops():  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Register all test primops before EvalState is created."""
    if not nanopynix.build_info()["capabilities"]["dynamic_primop_registration"]:
        return
    nanopynix.register_primop("test_add_one", 1, ["x"], "increment by 1", lambda x: x + 1)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_add", 2, ["x", "y"], "add two ints", lambda x, y: x + y)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_shout", 1, ["s"], "uppercase a string", lambda s: s.upper())  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_not", 1, ["b"], "negate a boolean", lambda b: not b)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_sum", 1, ["xs"], "sum a list of ints", lambda xs: sum(xs))  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_get", 2, ["attrs", "key"], "get attr from set", lambda attrs, key: attrs[key])  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_null", 1, ["_x"], "always returns null", lambda _x: None)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_half", 1, ["x"], "divide by 2 as float", lambda x: x / 2.0)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_range", 1, ["n"], "range(1, n+1)", lambda n: list(range(1, n + 1)))  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_make_attrs", 1, ["x"], "return { x = x; y = x+1; }", lambda x: {"x": x, "y": x + 1})  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_greet", 1, ["name"], "return greeting string", lambda name: f"Hello, {name}!")  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_double", 1, ["x"], "double an int", lambda x: x * 2)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_triple", 1, ["x"], "triple an int", lambda x: x * 3)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_overwrite", 1, ["x"], "first version", lambda x: x + 1)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_overwrite", 1, ["x"], "second version — wins", lambda x: x * 10)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_answer", 0, [], "the answer (zero arity)", lambda: 42)
    nanopynix.register_primop("test_add4", 4, ["a", "b", "c", "d"], "add 4 ints", lambda a, b, c, d: a + b + c + d)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix

    # Callable-returning primops (tests the Python-callable → Nix-function bridge).
    nanopynix.register_primop("test_return_lazy_42", 0, [], "returns a zero-arg lambda → 42", lambda: lambda: 42)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop(
        "test_attrs_property",
        1,
        ["n"],
        "returns { result = lambda: n * n; } (zero-arg, evaluated immediately)",
        lambda n: {"result": lambda: n * n},  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    )
    nanopynix.register_primop(
        "test_attrs_fn",
        1,
        ["x"],
        "returns { add = lambda y: x + y; } (1-arg callable)",
        lambda x: {"add": lambda y: x + y},  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    )
    nanopynix.register_primop(
        "test_attrs_fn2",
        1,
        ["x"],
        "returns { mul = lambda a, b: x * a * b; } (2-arg callable)",
        lambda x: {"mul": lambda a, b: x * a * b},  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    )
    nanopynix.register_primop(
        "test_closure_fn",
        2,
        ["n", "prefix"],
        "returns { greet = lambda name: prefix + ' ' + name + ' ' + str(n+2); }",
        lambda n, prefix: {"greet": lambda name: f"{prefix} {name} {n + 2}"},  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    )
    nanopynix.register_primop(
        "test_callable_curry",
        2,
        ["a", "b"],
        "returns a callable that takes one arg",
        lambda _a, _b: lambda x: x * 2,  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    )


@pytest.fixture(scope="session")
def eval_state(store: Any, init_expr: object, _register_test_primops: object) -> Any:  # noqa: ARG001
    """Create a session-scoped EvalState. Depends on _register_test_primops
    so that primops are registered before EvalState processes them."""
    return nanopynix.EvalState(store)


@pytest.fixture(scope="session", autouse=True)
def _enable_flakes():  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Enable flakes experimental feature for all tests."""
    nanopynix.enable_experimental_feature("flakes")
    nanopynix.enable_experimental_feature("nix-command")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_primops():  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Clear C++ primop registry at process exit to avoid segfault
    when nb::object destructors fire after Python finalization."""
    class _PrimopRegistryModule(Protocol):
        def _cleanup_primop_registry(self) -> None: ...

    atexit.register(cast("_PrimopRegistryModule", nanopynix_expr)._cleanup_primop_registry)  # type: ignore[reportPrivateUsage] -- intentional cleanup of C++ state at process exit
