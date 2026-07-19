"""Shared fixtures for nanopynix tests."""

from __future__ import annotations

import atexit
import functools
import inspect
import os
import re
import sys
from collections.abc import Awaitable, Callable, Generator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import anyio
import pytest
from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import util as nanopynix_util

import nanopynix

if TYPE_CHECKING:
    from collections.abc import Iterable

# Async tests can hang indefinitely on a wedged subprocess/pipe instead of
# failing (e.g. a Nix worker that stops responding never raises an error, it
# just never wakes the awaiting task up). Every async test gets wrapped in
# this deadline so a hang becomes a clear TimeoutError on that one test
# instead of the whole pytest run — and therefore the whole CI job — never
# returning. Override via NANOPYNIX_TEST_TIMEOUT for slower environments.
_ASYNC_TEST_TIMEOUT = float(os.environ.get("NANOPYNIX_TEST_TIMEOUT", "120"))


def _with_test_timeout(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    @functools.wraps(func)
    async def wrapper(*args: object, **kwargs: object) -> None:
        with anyio.fail_after(_ASYNC_TEST_TIMEOUT):
            await func(*args, **kwargs)

    return wrapper

_COVERAGE_SITECUSTOMIZE_DIR = str(Path(__file__).resolve().parent / "_coverage_subprocess")


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


def _enable_subprocess_coverage() -> None:
    """Let the multiprocessing-forkserver Nix worker report coverage too.

    pytest-cov sets COVERAGE_PROCESS_START when --cov is active, but that env
    var only takes effect in a subprocess whose interpreter imports a
    `sitecustomize` module. nanopynix's worker is forked from a forkserver
    helper process that is itself freshly exec'd, so it needs that shim
    discoverable via PYTHONPATH before it starts.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = [_COVERAGE_SITECUSTOMIZE_DIR, *([existing] if existing else [])]
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config):
    config.addinivalue_line(
        "markers",
        "live_gc: test performs destructive live Nix garbage collection",
    )
    config.addinivalue_line(
        "markers",
        "required_nix_version(min, max): require linked Nix in [min, max); use None for no bound",
    )
    if os.environ.get("COVERAGE_PROCESS_START"):
        _enable_subprocess_coverage()


def _nix_version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"(\d+(?:\.\d+)*)", value)
    if match is None:
        raise ValueError(f"cannot parse linked Nix version: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def _version_at_least(actual: tuple[int, ...], required: tuple[int, ...]) -> bool:
    width = max(len(actual), len(required))
    return actual + (0,) * (width - len(actual)) >= required + (0,) * (width - len(required))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001 -- hookspec signature requires config arg
    build_info: Any = nanopynix.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
    actual = _nix_version_tuple(build_info["nix_version"])
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

    for item in items:
        if isinstance(item, pytest.Function) and inspect.iscoroutinefunction(item.obj):
            item.obj = _with_test_timeout(item.obj)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(exitstatus: int | pytest.ExitCode) -> Generator[None]:
    """Force-exit once every other sessionfinish hook has run, terminal
    summary included.

    CPython's normal shutdown joins every non-daemon thread before the
    process can exit. A worker-pool executor thread or subprocess-handling
    thread that isn't perfectly torn down at session end otherwise leaves the
    whole pytest process (and therefore the CI job) hanging forever even
    though every test already finished and every report (coverage, junitxml,
    the terminal "N passed/failed" line) is already written.

    Coverage's own reporting runs earlier still, inside pytest_runtestloop.
    The terminal reporter's final summary line is printed from *its own*
    ``pytest_sessionfinish`` wrapper hook, after its ``yield``. Wrapper hooks
    nest like context managers: ``tryfirst=True`` makes ours the outermost
    one, so our ``yield`` runs first (deferring to every other hookimpl,
    wrapper or not) and our code after ``yield`` runs dead last, once
    everything else -- including the terminal summary -- has finished.
    """
    yield
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(exitstatus))


def pytest_runtest_setup(item: pytest.Item):
    if "live_gc" not in item.keywords:
        return
    if item.config.getoption("--run-live-gc") or os.environ.get("NANOPYNIX_RUN_LIVE_GC") == "1":
        return
    pytest.skip("destructive live GC test; pass --run-live-gc or NANOPYNIX_RUN_LIVE_GC=1 to run")


_store_mode_cache: str | None = None


class StorePathRecorder:
    """Persist default-store paths for deletion after pytest has exited."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def add(self, paths: Iterable[object]) -> None:
        """Append paths immediately so ``os._exit`` cannot lose them."""
        path = self._path
        if path is None:
            return
        entries = "".join(f"{store_path}\n" for store_path in paths)
        if not entries:
            return
        with path.open("a", encoding="utf-8") as file:
            file.write(entries)
            file.flush()
            os.fsync(file.fileno())


@pytest.fixture(scope="session")
def store_path_recorder() -> StorePathRecorder:
    """Record test outputs for the CI shell's post-pytest ``nix store delete``."""
    configured_path = os.environ.get("NANOPYNIX_TEST_DELETE_PATHS_FILE")
    if configured_path is None:
        return StorePathRecorder(None)
    path = Path(configured_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return StorePathRecorder(path)


def detected_store_mode() -> str:
    """Return how "auto" resolved a Nix store in this test process.

    "local" means a direct in-process ``LocalStore`` (Nix's own
    ``resolveStoreConfig`` picked this when the Nix state dir is writable by
    the current user — e.g. single-user Nix installs). "daemon" means Nix
    connected to a running ``nix-daemon`` over its Unix socket instead (e.g.
    multi-user Nix installs where the state dir isn't directly writable).
    Building/evaluating in "daemon" mode happens largely inside the separate
    daemon process, not this one, which changes the concurrency/GC exposure
    of in-process tests.
    """
    if _store_mode_cache is None:
        raise RuntimeError("store mode not yet detected; the _init fixture has not run")
    return _store_mode_cache


@pytest.fixture(scope="session", autouse=True)
def _init():  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Initialize libstore, enable flakes (needed by fetchers/flake tests)."""
    # These settings only affect a LocalStore constructed in this process; if
    # "auto" resolves to a daemon store instead (see detected_store_mode()),
    # the real build-users-group/sandboxing config lives in the daemon's own
    # nix.conf and these calls have no effect on it.
    nanopynix_util.set_setting("build-users-group", "")
    nanopynix_util.set_setting("require-drop-supplementary-groups", "false")
    nanopynix.init_libstore(load_config=False)

    global _store_mode_cache
    probe_store: Any = nanopynix.open_store()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
    _store_mode_cache = "daemon" if probe_store.get_uri() == "daemon" else "local"
    os.environ["NANOPYNIX_STORE_MODE"] = _store_mode_cache
    print(f"\n[nanopynix] auto store resolved to: {_store_mode_cache}")  # noqa: T201 -- CI visibility: which Nix store backend resolved is otherwise invisible in test output


@pytest.fixture(scope="session", autouse=True)
def _configure_worker_local_stores(_init: None) -> Iterator[None]:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Keep worker-owned ``local?root`` test stores independent of nixbld.

    The parent process's direct libstore initialization above already sets
    these values. RPC workers initialize libstore separately, however, and a
    multi-user installation otherwise makes their explicit temporary local
    stores chown pytest-owned directories to the nixbld group. ``NIX_CONFIG``
    is inherited when each worker starts, while the daemon remains configured
    as a real multi-user daemon.
    """
    original = os.environ.get("NIX_CONFIG")
    worker_settings = "build-users-group =\nrequire-drop-supplementary-groups = false"
    os.environ["NIX_CONFIG"] = f"{original}\n{worker_settings}" if original else worker_settings
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("NIX_CONFIG", None)
        else:
            os.environ["NIX_CONFIG"] = original


@pytest.fixture(scope="session")
def store_mode(_init: None) -> str:
    """'local' if "auto" resolved to a direct LocalStore, 'daemon' if via nix-daemon."""
    return detected_store_mode()


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
    build_info: Any = nanopynix.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
    if not build_info["capabilities"]["dynamic_primop_registration"]:
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
