"""Test helpers for pynixd functional tests."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import random
import shlex
import shutil
import stat
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import structlog
from environs import env

from pynixd import Server
from pynixd.instance import NixImplementation
from pynixd.store import LocalSocketStore
from pynixd.testing import clear_test_stash
from pynixd.types.ids import StoreId
from tests.nix_config import NixConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator, Sequence

try:
    from pyinstrument import Profiler
    from pyinstrument.renderers import ConsoleRenderer

    HAS_PYINSTRUMENT = True
except ImportError:
    Profiler: type | None = None
    ConsoleRenderer: type | None = None
    HAS_PYINSTRUMENT = False


# Structlog configuration
_session_start_time = time.monotonic()


def _abs_time_stamper(logger: Any, method_name: str, event_dict: Any) -> Any:
    """Store absolute monotonic time in the event dict for per-handler formatting."""
    event_dict["_abs_time"] = time.monotonic()
    return event_dict


def _relative_time_stamper(logger: Any, method_name: str, event_dict: Any) -> Any:
    """Compute timestamp relative to test_start_time (contextvar) or session start.

    The ``test_start_time`` contextvar is bound per-test by the ``test_log_file``
    fixture.  Background tasks in the shared pynixd server don't inherit this
    contextvar, so they fall back to ``_session_start_time``.  The per-test
    log handler (``_TestRelativeTimeHandler``) corrects these stale timestamps
    by recomputing from ``LogRecord.created``.
    """
    abs_time = event_dict.pop("_abs_time", None) or time.monotonic()
    start = event_dict.pop("test_start_time", None) or _session_start_time
    elapsed = abs_time - start
    seconds = int(elapsed)
    milliseconds = int((elapsed - seconds) * 1000)
    event_dict["timestamp"] = f"{seconds:03d}.{milliseconds:03d}"
    return event_dict


class _TestRelativeTimeHandler(logging.FileHandler):
    """File handler that recomputes timestamps relative to test start.

    Structlog's global chain renders timestamps relative to
    ``test_start_time`` from contextvars (or session start as fallback).
    But background tasks in the shared pynixd server don't inherit the
    current test's contextvars, so their timestamps would be stale.

    This handler patches the rendered message by replacing the leading
    ``SSS.MMM`` timestamp with one recomputed from ``LogRecord.created``
    (the wall-clock time of the log call) relative to when the test started.
    """

    _TS_LEN = 7  # "SSS.MMM" — just the timestamp digits and dot

    def __init__(self, test_start: float, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._test_start = test_start

    def emit(self, record: logging.LogRecord) -> None:
        if record.created:
            elapsed = record.created - self._test_start
            seconds = int(elapsed)
            milliseconds = int((elapsed - seconds) * 1000)
            ts = f"{seconds:03d}.{milliseconds:03d}"
            if record.msg and len(record.msg) >= self._TS_LEN:
                record.msg = ts + record.msg[self._TS_LEN :]
        super().emit(record)


structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.contextvars.merge_contextvars,
        _abs_time_stamper,
        _relative_time_stamper,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

log = structlog.get_logger(__name__)

# Ignore nar_integration tests during normal collection — they are run
# explicitly by path and their pytest_generate_tests is expensive.


def pytest_ignore_collect(collection_path: Path) -> bool | None:
    """Skip nar_integration directory during test collection."""
    if "nar_integration" in str(collection_path):
        return True
    return None


logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("aiosqlite").setLevel(logging.INFO)
logging.getLogger("pynixd.store.pool").setLevel(logging.INFO)


@contextmanager
def set_log_levels(levels: dict[str, int]):
    """Temporarily set logger levels, restoring them on exit."""
    saved = {}
    for name, level in levels.items():
        logger = logging.getLogger(name)
        saved[name] = logger.level
        logger.setLevel(level)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


NIX_BIN = env.path("NIX_BIN")
LIX_BIN = env.path("LIX_BIN", None) or NIX_BIN

CLIENT_BIN: Path = NIX_BIN  # Overridden in pytest_configure based on --client-bin


def pytest_addoption(parser):
    parser.addoption("--client-bin", choices=["nix", "lix"], default="nix")
    parser.addoption("--local-bin", choices=["nix", "lix"], default="nix")
    parser.addoption("--builder-bin", choices=["nix", "lix"], default="nix")


def pytest_configure(config):
    global CLIENT_BIN
    CLIENT_BIN = LIX_BIN if config.getoption("client_bin") == "lix" else NIX_BIN


def server_uri(server: Server) -> str:
    """Return server URI in format appropriate for the current client binary."""
    if CLIENT_BIN == LIX_BIN:
        return server.uri(NixImplementation.LIX)
    return server.uri(NixImplementation.NIX)


DEFAULT_SSH_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"


DEFAULT_NIX_CONFIG = NixConfig.for_test_store()


def get_test_store_kwargs(
    nix_config: NixConfig = DEFAULT_NIX_CONFIG,
    no_probe: bool = False,
    **kwargs,
) -> dict[str, Any]:
    """Return common kwargs for LocalSocketStore in tests.

    Args:
        nix_config: NixConfig to derive NIX_CONFIG env and daemon --option args from.
        no_probe: If True, skip build-based system/feature probing (saves ~4s per store).
            Supplies a default feature_matrix covering common test systems.
        **kwargs: Additional overrides passed through to LocalSocketStore.
    """
    extra_args = nix_config.to_daemon_args()
    if "extra_args" in kwargs:
        extra_args.extend(kwargs.pop("extra_args"))

    extra_env = kwargs.pop("extra_env", {})
    if "NIX_SSHOPTS" not in extra_env:
        extra_env["NIX_SSHOPTS"] = DEFAULT_SSH_OPTS
    if "NIX_CONFIG" not in extra_env:
        extra_env["NIX_CONFIG"] = nix_config.to_nix_config_env()

    res = {
        "nix_bin": str(NIX_BIN),
        "extra_args": extra_args,
        "extra_env": extra_env,
    }
    if no_probe:
        res["probe"] = False
        res.setdefault("feature_matrix", _NO_PROBE_FEATURE_MATRIX)
    res.update(kwargs)
    return res


STORE_PREFIX = Path("/tmp/pynixd-stores")
SESSION_STORE_PREFIX = Path("/tmp/pynixd-session-stores")
TEST_NIX = Path("tests/nix")


def ssh_admin_uri(server: Server) -> str:
    """Return an SSH URI for admin-user on the given server."""
    if CLIENT_BIN == LIX_BIN:
        return f"ssh-ng://admin-user@127.0.0.1?port={server.port}"
    return f"ssh-ng://admin-user@127.0.0.1:{server.port}"


def ssh_user_uri(server: Server) -> str:
    """Return an SSH URI for regular-user on the given server."""
    if CLIENT_BIN == LIX_BIN:
        return f"ssh-ng://regular-user@127.0.0.1?port={server.port}"
    return f"ssh-ng://regular-user@127.0.0.1:{server.port}"


def unix_session_uri(server: Server) -> str:
    """Return a Unix socket URI pointing to the session server."""
    socket_path = SESSION_STORE_PREFIX / "pynixd.sock"
    local_path = server.local_store.store_path
    return f"unix://{socket_path}?root={local_path}"


_NO_PROBE_FEATURE_MATRIX: dict[str, set[str]] = {
    "x86_64-linux": {
        "nixos-test",
        "benchmark",
        "big-parallel",
        "kvm",
        "ca-derivations",
        "recursive-nix",
    },
    "aarch64-linux": {
        "nixos-test",
        "benchmark",
        "big-parallel",
        "ca-derivations",
        "recursive-nix",
    },
}

_default_store_ids = {"local", "builder"}

_log_dir_key = pytest.StashKey[Path]()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create session-wide log directory and print its path.

    Also cleans up any leftover /tmp/pynixd-test-* dirs from previous runs
    (our tmp_path override uses this prefix) and the pytest-of-lillecarl
    garbage (pytest's own rm_rf fails on read-only Nix store files).
    """
    run_id = str(int(time.time()))
    log_dir = Path(f"/tmp/pynixd-logs/{run_id}")
    log_dir.mkdir(parents=True, exist_ok=True)
    session.config.stash[_log_dir_key] = log_dir

    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr:
        tr.write_line(f"\nIMPORTANT: Test run logs: {log_dir}")

    rmtree_robust_glob("/tmp/pynixd-test-*")
    rmtree_robust_glob("/tmp/pytest-of-lillecarl/*")


def _prune_client_processor(frame, options):
    """Custom pyinstrument processor to remove client-side subprocess execution."""
    if frame is None:
        return None

    for child in list(frame.children):
        if child.function and ("run_nix_build" in child.function or "run_subproc" in child.function):
            child.remove_from_parent()
        else:
            _prune_client_processor(child, options)

    return frame


def _make_profile_filename(request: pytest.FixtureRequest) -> str:
    """Generate a short identifiable filename for a profile."""
    node = request.node
    parts = [node.path.name]
    if hasattr(node, "name") and node.name != node.path.name:
        full_name = node.name
        if "[" in full_name:
            param = full_name.split("[", 1)[1].rstrip("]")
            parts.append(param)
    return "pynixd-profile-" + "-".join(parts) + ".txt"


def _record(request: pytest.FixtureRequest, label: str, **kwargs: Any) -> None:
    """Record benchmark results in the test stash."""
    log.info("benchmark_result", label=label, **kwargs)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print log directory path at the end of the test run."""
    log_dir = config.stash.get(_log_dir_key, None)
    if log_dir:
        terminalreporter.write_line(f"\nIMPORTANT: Test run logs: {log_dir}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    """Automatically wrap async tests in asyncio.timeout.

    This provides better diagnostic information (asyncio tracebacks) than the
    nuclear 'pytest-timeout' option by triggering 5s earlier.
    """
    # Prefer merged value from pytest-timeout if available
    try:
        default_timeout = config.getvalue("timeout")
    except (AttributeError, ValueError):
        default_timeout = None

    if default_timeout is None:
        default_timeout = os.environ.get("PYTEST_TIMEOUT")
    if default_timeout is None:
        default_timeout = config.getini("timeout")

    try:
        default_timeout = float(default_timeout) if default_timeout else 120.0
    except ValueError:
        default_timeout = 120.0

    for item in items:
        # pytest.Item doesn't officially expose 'obj' in its type definition,
        # but it exists for Function nodes.
        if (
            isinstance(item, pytest.Function)
            and asyncio.iscoroutinefunction(item.obj)
            and not getattr(item.obj, "_pynixd_timeout_wrapped", False)
        ):
            item.obj = _wrap_with_asyncio_timeout(item, default_timeout)
            item.obj._pynixd_timeout_wrapped = True  # type: ignore[reportAttributeAccessIssue]

    # Lix: skip tests requiring CA/dynamic derivations when either the
    # client, local store, or builder store uses Lix (Lix's daemon cannot
    # parse floating CA outputs in BuildDerivation).
    client_bin = config.getoption("client_bin", "nix")
    local_bin = config.getoption("local_bin", "nix")
    builder_bin = config.getoption("builder_bin", "nix")
    if "lix" in (client_bin, local_bin, builder_bin):
        for item in items:
            if item.get_closest_marker("ca_derivations"):
                item.add_marker(pytest.mark.skip(reason="Not supported with Lix"))


def _wrap_with_asyncio_timeout(item: pytest.Function, default_timeout: float):
    original_func = item.obj

    @functools.wraps(original_func)
    async def wrapped(*args, **kwargs):
        timeout_mark = item.get_closest_marker("timeout")
        seconds = float(timeout_mark.args[0] if timeout_mark else default_timeout)

        # Skip if timeout is 0 (disabled)
        if seconds <= 0:
            return await original_func(*args, **kwargs)

        # Wrap in asyncio.timeout with a 5s buffer to trigger before pytest-timeout.
        # Ensure we have at least 1s if the original timeout was very short.
        timeout_val = max(1.0, seconds - 5.0)

        try:
            async with asyncio.timeout(timeout_val):
                return await original_func(*args, **kwargs)
        except TimeoutError:
            log.exception(
                "test_timeout_triggered",
                test=item.nodeid,
                timeout=seconds,
                effective=timeout_val,
            )
            # Re-raise to let pytest handle the failure
            raise

    return wrapped


@pytest.fixture(autouse=True)
def clear_instrumentation():
    """Clear internal test stash before each test."""

    clear_test_stash()
    return


@pytest.fixture(scope="session")
def test_log_dir(request: pytest.FixtureRequest) -> Path:
    """Return the session-wide log directory."""
    return request.session.config.stash[_log_dir_key]


def get_log_file_path(log_dir: Path, item: Any) -> Path:
    """Generate a consistent log file path: log_dir/test_file::test_func.log"""
    file_stem = item.path.stem
    # Only replace "/" as it's the path separator
    safe_name = item.name.replace("/", "_")
    return log_dir / f"{file_stem}::{safe_name}.log"


@pytest.fixture(autouse=True)
def test_log_file(request: pytest.FixtureRequest, test_log_dir: Path):
    """Redirect all structlog output for this test to its own log file."""
    log_file = get_log_file_path(test_log_dir, request.node)

    structlog.contextvars.bind_contextvars(test_start_time=time.monotonic())

    test_start = time.time()
    handler = _TestRelativeTimeHandler(test_start, log_file)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)

    yield log_file

    root_logger.removeHandler(handler)
    root_logger.setLevel(old_level)
    handler.close()


@pytest.fixture(autouse=True)
def _fixed_test_ts():
    """Pin PYNIXD_TEST_TS for each test so build+eval get consistent .drv paths."""
    ts = str(int(time.time()))
    original = os.environ.get("PYNIXD_TEST_TS")
    os.environ["PYNIXD_TEST_TS"] = ts
    yield
    if original is None:
        os.environ.pop("PYNIXD_TEST_TS", None)
    else:
        os.environ["PYNIXD_TEST_TS"] = original


@pytest.fixture(autouse=True)
async def profiler(request: pytest.FixtureRequest, test_log_dir: Path):
    """Profile every test and save to a .pyinstrument file."""
    if not HAS_PYINSTRUMENT:
        yield None
        return

    assert Profiler is not None
    p = Profiler(async_mode="enabled")
    p.start()

    yield p

    if p is not None:
        if p.is_running:
            p.stop()

        session = p.last_session
        if session:
            log_file = get_log_file_path(test_log_dir, request.node)
            profile_file = log_file.with_suffix(".pyinstrument")

            assert ConsoleRenderer is not None
            renderer = ConsoleRenderer(unicode=True, color=False)
            renderer.processors.insert(0, _prune_client_processor)

            with profile_file.open("w") as f:
                content = renderer.render(session)
                f.write(content)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    """
    Write captured output and failure details to log file, suppress console display.
    """
    outcome = yield
    report = outcome.get_result()

    if report.when == "call" and report.failed:
        log_dir = item.config.stash.get(_log_dir_key, None)
        if log_dir:
            log_file = get_log_file_path(log_dir, item)
            with log_file.open("a") as f:
                if report.longrepr:
                    f.write("\n--- Failure details ---\n")
                    f.write(str(report.longrepr))
                if report.capstdout:
                    f.write("\n--- Captured stdout ---\n")
                    f.write(report.capstdout)
                if report.capstderr:
                    f.write("\n--- Captured stderr ---\n")
                    f.write(report.capstderr)

            # Replace longrepr with short message for console
            report.longrepr = f"FAILED (see log file: {log_file})"


def rmtree_robust(path: str | Path) -> None:
    """Recursively remove a directory or file, unsetting read-only bits as needed."""
    path = Path(path)
    if not path.exists():
        return

    if path.is_dir():

        def handle_errors(func, path, _excinfo):
            try:
                Path(path).chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                func(path)
            except Exception:
                pass

        shutil.rmtree(path, onerror=handle_errors)
    else:
        try:
            path.unlink()
        except PermissionError:
            try:
                path.chmod(stat.S_IWRITE | stat.S_IREAD)
                path.unlink()
            except Exception:
                pass
        except Exception:
            pass


def rmtree_robust_glob(pattern: str) -> None:
    """Remove all directories matching a glob pattern."""
    import glob

    # Use glob.glob for absolute paths, which Path().glob doesn't handle well
    for path_str in glob.glob(pattern):  # noqa: PTH207
        rmtree_robust(Path(path_str))


@pytest.fixture(autouse=True)
def cleanup_stores():
    """Remove any leftover test stores before and after each test.

    Covers our STORE_PREFIX (/tmp/pynixd-stores/) and the tmp_path override
    prefix (/tmp/pynixd-test-*) — both may contain read-only Nix store files.
    """
    yield
    rmtree_robust_glob(f"{STORE_PREFIX}/*")
    rmtree_robust_glob("/tmp/pynixd-test-*")


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Generator[Path]:
    """Override pytest's tmp_path to use rmtree_robust for teardown.

    pytest's default tmp_path uses tmp_path_factory which registers with
    pytest's session-scoped cleanup (shutil.rmtree) — this fails on read-only
    Nix store files. Instead we create dirs under a dedicated prefix and
    clean them with rmtree_robust which handles read-only files.
    """
    suffix = f"{request.node.name}-{random.getrandbits(32):08x}"
    path = Path(f"/tmp/pynixd-test-{suffix}")
    path.mkdir(parents=True, exist_ok=True)
    yield path
    with suppress(Exception):
        rmtree_robust(path)


@pytest.fixture(autouse=True)
async def cleanup_extra_stores(pynixd_server: Server | tuple | None):
    """Remove non-default stores added by tests between each test."""
    yield
    if pynixd_server is None:
        return

    # Handle cases where pynixd_server is a tuple (integration tests)
    actual_server = pynixd_server[0] if isinstance(pynixd_server, tuple) else pynixd_server
    if not hasattr(actual_server, "stores"):
        return

    extra_ids = [sid for sid in actual_server.stores if sid not in _default_store_ids]

    for sid in extra_ids:
        store = actual_server.stores[sid]
        store_path = store.store_path
        await actual_server.remove_store(sid)
        if store_path and str(store_path).startswith(str(SESSION_STORE_PREFIX)):
            await asyncio.to_thread(rmtree_robust, store_path)


async def run_subproc(
    cmd: Sequence[str | Path],
    verbose: bool = True,
    expected_retcode: int | None = 0,
    nix_config: NixConfig | dict[str, str] | None = None,
    **kwargs,
) -> tuple[int, str, str, str]:
    """Run a command, streaming stdout/stderr through structlog in real-time.

    Args:
        cmd: Command and arguments to run
        verbose: If True, stream output to structlog in real-time
        expected_retcode: If not None, raise if return code doesn't match. Defaults to 0.
        nix_config: NixConfig object or dict for NIX_CONFIG env var.
        **kwargs: Additional arguments passed to create_subprocess_exec

    Returns:
        tuple of (returncode, stdout, stderr, combined)
    """
    run_env = kwargs.pop("env", {})
    if "NIX_SSHOPTS" not in run_env:
        run_env["NIX_SSHOPTS"] = DEFAULT_SSH_OPTS

    if isinstance(nix_config, NixConfig):
        config_str = nix_config.to_nix_config_env()
    elif nix_config is not None:
        default_config = {
            "substituters": "https://cache.nixos.org unix:///nix/var/nix/daemon-socket/socket?root=/",
        }
        merged = default_config | nix_config
        config_str = "\n".join(f"{k} = {v}" for k, v in merged.items())
    else:
        config_str = DEFAULT_NIX_CONFIG.to_nix_config_env()

    if "NIX_CONFIG" in run_env:
        run_env["NIX_CONFIG"] = f"{run_env['NIX_CONFIG']}\n{config_str}"
    else:
        run_env["NIX_CONFIG"] = config_str

    str_cmd = [str(c) for c in cmd]
    log.debug("run_subproc", cmd=shlex.join(str_cmd), env=run_env)
    proc = await asyncio.create_subprocess_exec(
        *str_cmd,
        env=os.environ.copy() | run_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )

    stdout: list[str] = []
    stderr: list[str] = []
    stdboth: list[str] = []

    async def stream(name: str, accumulator: list[str], pipe) -> None:
        while True:
            line = await pipe.readline()
            decoded_line = line.decode()
            accumulator.append(decoded_line)
            stdboth.append(decoded_line)
            if not line:
                break
            if verbose:
                log.info(name, message=decoded_line.rstrip())

    await asyncio.gather(
        stream("stdout", stdout, proc.stdout),
        stream("stderr", stderr, proc.stderr),
    )
    rc = proc.returncode if proc.returncode is not None else 0
    if expected_retcode is not None and rc != expected_retcode:
        raise RuntimeError(
            f"Command failed with rc={rc} (expected {expected_retcode}):\n{''.join(stdboth)}",
        )
    return (
        rc,
        "".join(stdout),
        "".join(stderr),
        "".join(stdboth),
    )


@pytest.fixture(scope="session")
def nix_env() -> dict[str, str]:
    """Environment variables for nix subprocess calls."""
    return os.environ.copy()


SESSION_SSH_PORT = 0
SESSION_HTTP_PORT = 0
SESSION_HTTP_USER = "testuser"
SESSION_HTTP_PASS = "testpass"
SESSION_NIX_CONFIG = NixConfig.for_test_store(
    experimental_features=(
        "nix-command",
        "flakes",
        "read-only-local-store",
        "ca-derivations",
        "dynamic-derivations",
        "recursive-nix",
    ),
)


@pytest.fixture(scope="session", autouse=True)
async def pynixd_server(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[Server]:
    """Session-scoped shared pynixd server (autouse)."""
    # Check if any test in the session needs a session server.
    # Actually, autouse session fixtures can't easily check markers of the current test.
    # But we can check the command line or just let it run.

    # Wait! I'll make it NOT autouse, but requested by functional tests.
    # Or just let it run but don't bind to it if not needed.

    local_path = SESSION_STORE_PREFIX / "local"
    builder_path = SESSION_STORE_PREFIX / "builder"
    socket_path = SESSION_STORE_PREFIX / "pynixd.sock"

    rmtree_robust(local_path)
    rmtree_robust(builder_path)
    rmtree_robust(socket_path)

    local_bin = LIX_BIN if request.config.getoption("local_bin") == "lix" else NIX_BIN
    builder_bin = LIX_BIN if request.config.getoption("builder_bin") == "lix" else NIX_BIN

    local_store = LocalSocketStore(
        store_id=StoreId("local"),
        store_path=local_path,
        **get_test_store_kwargs(nix_config=SESSION_NIX_CONFIG, nix_bin=str(local_bin)),
    )
    builder_store = LocalSocketStore(
        store_id=StoreId("builder"),
        store_path=builder_path,
        **get_test_store_kwargs(nix_config=SESSION_NIX_CONFIG, nix_bin=str(builder_bin)),
    )

    upload_dir = tmp_path_factory.mktemp("http-uploads")

    async with Server(
        local_store=local_store,
        stores={StoreId("builder"): builder_store},
        ssh_port=SESSION_SSH_PORT,
        http_port=SESSION_HTTP_PORT,
        unix_path=socket_path,
        http_upload_dir=upload_dir,
        http_user=SESSION_HTTP_USER,
        http_pass=SESSION_HTTP_PASS,
        admin_users={"admin-user"},
    ) as server:
        yield server
