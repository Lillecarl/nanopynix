"""The session fixtures that any suite touching Nix needs, as a pytest plugin.

Register it from a ``conftest.py``::

    pytest_plugins = ("nanopynix_testing.fixtures",)

**These fixtures were in the repository's root ``tests/conftest.py`` until
issue #130.** A conftest reaches the directory that holds it and everything
below, so a suite that moves into its own project with its own rootdir loses
every one of them. A plugin has no such rule, which is why this module exists.

The measurement that put them here, by counting fixture requests in each test
signature:

======================= =============== ===========
fixture                 nanopynix suite pynix suite
======================= =============== ===========
``store``               94              2
``eval_state``          84              0
``store_seeded_path``   25              0
``nixpkgs_path``        1               25
``init_expr``           9               0
``store_path_recorder`` 4               0
``repo_root``           0               2
======================= =============== ===========

Two suites use the set, so it is shared. The single-suite entries travel with
it rather than splitting, because each one depends on the autouse
initialisation below, and a fixture separated from the initialisation it needs
is a fixture that fails in a new rootdir for a reason nobody can read.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest
from nanopynix_bindings import expr as nanopynix_expr, util as nanopynix_util

import nanopynix
from nanopynix.inproc import _impl as inproc_impl
from nanopynix.settings import DEFAULT_EXPERIMENTAL_FEATURES
from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Iterator

    from nanopynix_testing.nix_environment import NixTestEnvironment

#: The file that marks the top of the checkout. `repo_root` walks up for it.
_REPO_MARKER = "flake.nix"


def pytest_addoption(parser: pytest.Parser) -> None:
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
    # The concurrency soak. See nanopynix_testing.soak for what these drive.
    parser.addoption(
        "--soak-seed",
        type=int,
        default=0,
        help="seed that picks the soak schedule; the same seed replays the same composition",
    )
    parser.addoption(
        "--soak-lanes",
        type=int,
        default=8,
        help="how many soak tests run at once",
    )
    parser.addoption(
        "--soak-report",
        default=None,
        help="write the soak schedule and outcomes to this JSON file",
    )
    parser.addoption(
        "--soak-manifest",
        default=None,
        help="replay the composition recorded in this JSON file, ignoring --soak-seed",
    )


def _sanitizer_runtime_loaded() -> bool:
    """Tell whether this process has a sanitizer runtime mapped.

    The map of the process answers this, and no environment variable does.
    `nanopynix/tests.nix` preloads the runtime, but a developer can also build
    a sanitizer venv and call `pytest` in it.
    """
    try:
        maps = Path("/proc/self/maps").read_text()
    except OSError:
        # Not Linux, or no procfs. The check below then does nothing, which is
        # the same behaviour as before this check existed.
        return False
    return any(name in maps for name in ("libasan.so", "libtsan.so", "libubsan.so"))


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_gc: test performs destructive live Nix garbage collection",
    )
    # A sanitizer writes its report with `write(2)` on file descriptor 2, and
    # `--capture=fd` points that descriptor at a temporary file. `halt_on_error=1`
    # then aborts the process before pytest restores the descriptor and prints
    # the buffer, so the whole run prints *nothing* and exits 1. Measured on
    # 2026-08-03, with issue #34 restored on purpose under the ASAN build.
    #
    # `--capture=sys` replaces `sys.stderr` alone and leaves the descriptor, so
    # the report reaches the terminal and every other capture behaviour stays.
    # This refuses the run rather than correcting it, because the capture method
    # is fixed before any conftest loads: the capture plugin is a hookwrapper
    # around `pytest_load_initial_conftests`, and it builds the capture manager
    # from the command line before the inner hook loads this file.
    if config.option.capture == "fd" and _sanitizer_runtime_loaded():
        raise pytest.UsageError(
            "a sanitizer runtime is loaded, and --capture=fd hides its report: "
            "the process aborts before pytest prints the captured output, so "
            "the run reports nothing at all. Pass --capture=sys, which keeps "
            "file descriptor 2, or --capture=no."
        )


def pytest_runtest_setup(item: pytest.Item) -> None:
    if "live_gc" not in item.keywords:
        return
    if item.config.getoption("--run-live-gc") or os.environ.get("NANOPYNIX_RUN_LIVE_GC") == "1":
        return
    pytest.skip("destructive live GC test; pass --run-live-gc or NANOPYNIX_RUN_LIVE_GC=1 to run")


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


@pytest.fixture(autouse=True)
def no_process_wide_nix_setting_survives_a_test(request: pytest.FixtureRequest) -> Generator[None]:
    """Fail the test that leaves a Nix setting behind in this process.

    **A setting written through an inproc session is written here.**
    ``Session.set_settings`` reaches Nix's ``globalConfig``, which belongs to
    the pytest process, so every test that runs afterwards reads what the last
    writer left. Nothing said so, and the cost of that silence was a day:
    ``test_config_flow.py`` left ``keep-going`` on, and
    ``pynix/tests/test_copy.py`` then failed in twelve CI jobs and passed
    whenever it ran alone. ``keep-going`` makes Nix end a failing copy quietly
    -- ``Store::addMultipleToStore`` counts the failure and returns, and
    ``copyPaths`` never reads that count.

    This names the test that leaked, rather than leaving a later and unrelated
    test to fail for it. It found seven, and the seventh was a helper that
    sixteen tests share.

    **It reports and does not restore.** Restoring needs a session, and
    ``set_settings`` is refused while a store or an evaluator is open, so the
    fixture could not always do it. Naming the culprit needs neither.

    **A ``forked`` test is exempt, and that is the whole point of the marker.**
    Its writes land in a child that exits, so they reach no other test.
    ``test_support.plugin.pytest_collection_modifyitems`` runs those first,
    before anything has initialised Nix here, which is what makes the fork
    clean. ``request.keywords`` holds every marker of the item, and it is what
    pyright can read: ``request.node`` is untyped in pytest's own stubs.

    **This module and not ``test_support.plugin``.** That one holds what every
    suite shares *and that names no Nix concept*, and ``nix/checks.nix``
    enforces it with a venv that has no nanopynix in it. This names several.
    It also lands in the right five suites for free: the `pytest.ini` files
    that register this plugin are exactly the ones with nanopynix, so
    ``libpynix`` and ``grpclib-transports`` never load it.

    Measured before it went in: ``list_settings()`` costs 72 us and spawns no
    thread, so this is 354 ms across a 2452-test run and leaves the process
    single-threaded -- which it must, or the ``forked`` tests that run first
    would be forking a dirty process. Issue #282.
    """
    if "forked" in request.keywords:
        yield
        return

    # **The first session of a process applies its own settings here, and that
    # is the session and not the test.** `NixTestEnvironment` gives every
    # session its substituters and its experimental features, and those reach
    # `globalConfig` when Nix is constructed. Measured: the first test to open
    # one reported `substituters`, `experimental-features` and `nix-path`
    # changing, and no test after it reported them again. So a test that finds
    # Nix uninitialised is exempt, and every later one is judged.
    #
    # `_process_guard` is private, and this is the signal it owns. The public
    # surface has `init_libstore`, which would *cause* the thing this needs to
    # observe.
    guard = inproc_impl._process_guard  # type: ignore[reportPrivateUsage] -- see the comment above  # noqa: SLF001 -- see the comment above
    was_initialised = guard._initialized_pid is not None  # type: ignore[reportPrivateUsage] -- see above  # noqa: SLF001 -- see above
    before = nanopynix.list_settings()
    yield
    if not was_initialised:
        return
    after = nanopynix.list_settings()

    changed = sorted(
        f"{name}: {before.get(name)!r} -> {value!r}" for name, value in after.items() if before.get(name) != value
    )
    if changed:
        pytest.fail(
            "this test left a Nix setting behind in the pytest process, where every test after it reads "
            "the value. Mark it `@pytest.mark.forked`, which runs it in a child before anything here has "
            "initialised Nix, or put the setting back.\n  " + "\n  ".join(changed),
            pytrace=False,
        )


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


@pytest.fixture(scope="session", autouse=True)
def _init() -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Initialize libstore without opening a host-selected store."""
    nanopynix_util.set_setting("build-users-group", "")
    nanopynix_util.set_setting("require-drop-supplementary-groups", "false")
    nanopynix.init_libstore(load_config=False)


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
def store(l1_nix_environment: NixTestEnvironment) -> Iterator[Any]:
    """Open this run's isolated local/native-daemon Store.

    Backed by ``l1_nix_environment`` (see ``nanopynix_testing.nix_environment``),
    never the host Nix installation's own store. Sync tests cannot depend on
    the async ``shared_nix_environment``, hence the dedicated sync fixture.
    """
    opened = nanopynix.open_store(l1_nix_environment.store_uri)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture(scope="session")
def store_seeded_path(store: Any, l1_nix_environment: NixTestEnvironment) -> Any:
    """A StorePath added directly into this session's isolated Store.

    Store-backed tests must not derive fixture paths from the host Nix
    installation (see TODO.md); this gives them a known-valid path instead.
    """
    source = l1_nix_environment.root.parent / "l1-store-fixture.txt"
    source.write_text("nanopynix L1 binding fixture\n", encoding="utf-8")
    return store.add_to_store(str(source), name="nanopynix-l1-fixture", method="flat", hash_algo="sha256")


@pytest.fixture(scope="session")
def init_expr() -> None:
    """Initialize libexpr (once per session)."""
    nanopynix.init_libexpr()


@pytest.fixture(scope="session", autouse=True)
def _register_test_primops() -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Register all test primops before EvalState is created."""
    build_info: Any = nanopynix.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
    if not build_info["capabilities"]["dynamic_primop_registration"]:
        return
    nanopynix.register_primop("test_add_one", 1, ["x"], "increment by 1", lambda x: x + 1)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_add", 2, ["x", "y"], "add two ints", lambda x, y: x + y)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_shout", 1, ["s"], "uppercase a string", lambda s: s.upper())  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_not", 1, ["b"], "negate a boolean", lambda b: not b)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    nanopynix.register_primop("test_sum", 1, ["xs"], "sum a list of ints", sum)
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
    nanopynix.register_primop("test_identity_string", 1, ["s"], "return the string arg unchanged", lambda s: s)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix

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
def eval_state(store: Any, init_expr: object, _register_test_primops: object) -> Any:  # noqa: ARG001 -- init_expr and _register_test_primops are ordering-only fixture dependencies, never read here
    """Create a session-scoped EvalState. Depends on _register_test_primops
    so that primops are registered before EvalState processes them."""
    return nanopynix.EvalState(store)


@pytest.fixture(scope="session", autouse=True)
def _enable_default_experimental_features() -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Enable nanopynix's default experimental features before any store is opened.

    ``nanopynix.init_libstore`` already does this, for a reason
    documented there: enabling ``ca-derivations`` *after* a ``LocalStore`` was
    constructed without it aborts the process (SIGABRT, not a catchable error).
    This fixture is not a second remedy, it is an ordering guarantee -- pytest
    does not promise that the fixture calling ``init_libstore`` runs before
    every other fixture that opens a store, and this one is autouse and
    session-scoped. Enabling a feature twice is a no-op, so the overlap is free.
    """
    for feature in DEFAULT_EXPERIMENTAL_FEATURES:
        nanopynix.enable_experimental_feature(feature)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_primops() -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Clear C++ primop registry at process exit to avoid segfault
    when nb::object destructors fire after Python finalization."""

    class _PrimopRegistryModule(Protocol):
        def _cleanup_primop_registry(self) -> None: ...

    atexit.register(cast("_PrimopRegistryModule", nanopynix_expr)._cleanup_primop_registry)  # type: ignore[reportPrivateUsage] -- intentional cleanup of C++ state at process exit  # noqa: SLF001 -- same reason


@pytest.fixture(scope="session")
def repo_root(pytestconfig: pytest.Config) -> Path:
    """The checkout root.

    **It walks up from the rootdir, and it does not read this file's own
    path.** This module installs into a venv, so ``__file__`` points at a store
    path or a site-packages directory and says nothing about the checkout. The
    rootdir is a directory of the checkout in every run, whether pytest starts
    at the repository or at one project inside it, so the walk finds the same
    root either way.

    Three suites need it: pynix, nanopynix's error-boundary tests, and anything
    that evaluates this repository's own flake.
    """
    for candidate in (pytestconfig.rootpath, *pytestconfig.rootpath.parents):
        if (candidate / _REPO_MARKER).is_file():
            return candidate
    raise RuntimeError(f"no {_REPO_MARKER} at or above the pytest rootdir {pytestconfig.rootpath}")


@pytest.fixture(scope="module")
async def nixpkgs_path(repo_root: Path) -> str:
    """The pinned nixpkgs, realised once per module.

    Shared for the same reason as ``repo_root``: it used to be copy-pasted per
    suite, which meant every copy paid the evaluation separately.
    """
    result = await run_process(["nix", "eval", "--impure", "--raw", "--file", str(repo_root), "pkgs.path"])
    if result.returncode != 0:
        raise RuntimeError(f"nix eval of pkgs.path {result.describe()}")
    return result.stdout.strip()
