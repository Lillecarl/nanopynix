"""Integration test helpers for pynixd."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import structlog
from environs import Env

from pynixd.store import LocalSocketStore, Store


def rmtree_robust(path: str | Path) -> None:
    """Recursively remove a directory, unsetting read-only bits as needed."""
    path = Path(path)
    if not path.exists():
        return

    def handle_errors(func, path, _excinfo):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(path)
        except Exception:
            pass

    shutil.rmtree(path, onerror=handle_errors)


# ── Logging Redirection ──────────────────────────────────────────


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create session-wide log directory and print its path."""
    run_id = time.strftime("%Y%m%d-%H%M%S")
    path = Path(tempfile.gettempdir()) / f"pynixd-testrun-{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    
    # Store path in session for fixtures to use
    session.stash[_log_dir_key] = path
    
    # Use terminalreporter to print even when capture is on
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr:
        tr.write_line(f"\n🚀 Test run logs: {path}")


_log_dir_key = pytest.StashKey[Path]()


@pytest.fixture(scope="session")
def test_run_dir(request: pytest.FixtureRequest) -> Path:
    """Return the session-wide log directory."""
    return request.session.stash[_log_dir_key]


@pytest.fixture(autouse=True)
def test_log(request: pytest.FixtureRequest, test_run_dir: Path):
    """Redirect all logging for this specific test to a file."""
    # Create filename from test name and parameters
    node = request.node
    safe_name = node.name.replace("/", "_").replace(":", "_").replace("[", "_").replace("]", "_")
    log_file = test_run_dir / f"{safe_name}.log"

    # Set up file handler
    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Temporarily lower root level to DEBUG if it's higher
    old_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)

    yield log_file

    # Cleanup
    root_logger.removeHandler(handler)
    root_logger.setLevel(old_level)
    handler.close()


# Initial base configuration for stdlib logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(message)s",
    handlers=[], # Don't add default console handler
)

# Structlog configuration with standard library integration
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Use a plain renderer for files, or ConsoleRenderer if we want colors in logs
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# Silence noisy loggers
for _op in (
    "QueryPathInfo",
    "QueryValidPaths",
    "IsValidPath",
    "NarFromPath",
    "SetOptions",
    "QueryAllValidPaths",
    "AddToStore",
):
    logging.getLogger(f"pynixd.op.{_op}").setLevel(logging.INFO)
logging.getLogger("asyncssh").setLevel(logging.WARNING)
logging.getLogger("pynixd.store.pool").setLevel(logging.WARNING)
logging.getLogger("pynixd.scheduler.pass").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("pynixd.stderr").setLevel(logging.INFO)
logging.getLogger("asyncio").setLevel(logging.WARNING)

log = structlog.get_logger(__name__)

env = Env()

TEST_NIX = env.path("PYNIXD_TEST_NIX", Path("test.nix"))

LIX_BIN = env.path("LIX_BIN", Path("nix"))
NIX_BIN = env.path("NIX_BIN", Path("nix"))


# ── Benchmark infrastructure ──────────────────────────────────────


@dataclass
class BenchResult:
    label: str
    columns: dict[str, str]
    baselines: dict[str, str] = field(default_factory=dict)
    profile_path: str | None = None


_bench_key = pytest.StashKey[list[BenchResult]]()


def _record(
    request: pytest.FixtureRequest,
    label: str,
    baselines: dict[str, str] | None = None,
    profile_path: str | None = None,
    **columns: str,
) -> None:
    """Record a benchmark result.

    All column values are pre-formatted strings with units.
    Baselines should also be formatted strings (e.g. "30.1s (+50.2%)").
    """
    result = BenchResult(
        label=label,
        columns=columns,
        baselines=baselines or {},
        profile_path=profile_path,
    )
    results = request.config.stash.setdefault(_bench_key, [])
    results.append(result)


def _prune_client_processor(frame, options):
    """Custom pyinstrument processor to remove client-side subprocess execution."""
    if frame is None:
        return None

    for child in list(frame.children):
        if child.function and "run_nix_build" in child.function:
            child.remove_from_parent()
        else:
            _prune_client_processor(child, options)

    return frame


def _make_profile_filename(request: pytest.FixtureRequest) -> str:
    """Generate a short identifiable filename for a profile.

    E.g. pynixd-profile-test_bench_build.py-unix-lix-100-0.txt
    """
    node = request.node
    parts = [node.path.name]
    if hasattr(node, "name") and node.name != node.path.name:
        full_name = node.name
        if "[" in full_name:
            param = full_name.split("[", 1)[1].rstrip("]")
            parts.append(param)
    return "pynixd-profile-" + "-".join(parts) + ".txt"


def _print_bench_summary(
    terminalreporter: pytest.TerminalReporter,
    config: pytest.Config,
) -> None:
    """Print unified benchmark summary tables using rich."""
    from io import StringIO

    from rich.console import Console
    from rich.table import Table

    results: list[BenchResult] = config.stash.get(_bench_key, [])
    if not results:
        return

    # Group by column signature so similar benchmarks share a table
    groups: dict[tuple[str, ...], list[BenchResult]] = {}
    for r in results:
        sig = tuple(sorted(r.columns.keys()))
        groups.setdefault(sig, []).append(r)

    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)

    for cols, group_results in groups.items():
        table = Table(title="Benchmark Results", show_lines=True)
        table.add_column("Label", style="cyan", no_wrap=True)
        for col in cols:
            table.add_column(col, style="green", no_wrap=True)
        table.add_column("Baselines", style="yellow", no_wrap=True)

        for r in group_results:
            row_values = [r.columns.get(col, "") for col in cols]
            baselines_text = "\n".join(r.baselines.values()) if r.baselines else ""
            table.add_row(r.label, *row_values, baselines_text)

        console.print()
        console.print(table)

    profile_results = [r for r in results if r.profile_path]
    if profile_results:
        console.print()
        console.print("Profiles:", style="bold")
        for r in profile_results:
            console.print(f"  {r.label} → {r.profile_path}")

    terminalreporter.write_line(buf.getvalue())


@pytest.fixture(scope="session", autouse=True)
def cleanup_bench_paths():
    """Ensure large benchmark artifacts are deleted before tests run."""
    log.info("Cleaning up old benchmark paths")
    try:
        result = subprocess.run(
            f"{NIX_BIN} path-info -rS /nix/store | grep bench-100mb | cut -f1",
            shell=True,
            capture_output=True,
            text=True,
        )
        for p in result.stdout.splitlines():
            if p.strip():
                log.info("Deleting old benchmark path", path=p)
                subprocess.run(
                    [str(NIX_BIN), "store", "delete", p], capture_output=True
                )
    except Exception as e:
        log.warning("Could not cleanup benchmark paths", error=e)


def get_current_system() -> str:
    """Return nix system string (e.g. x86_64-linux)."""
    return (
        subprocess.check_output(
            [
                str(NIX_BIN),
                "eval",
                "--raw",
                "--impure",
                "--expr",
                "builtins.currentSystem",
            ]
        )
        .decode()
        .strip()
    )


def _run_subprocess_with_timeout(
    cmd: list[str], env: dict[str, str], timeout: float = 60.0
) -> tuple[int, str, str]:
    """Run subprocess and return (rc, stdout, stderr), raising on timeout."""
    try:
        res = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")


async def run_process_async(
    cmd: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a subprocess asynchronously and return (rc, stdout, stderr)."""
    res = await asyncio.create_subprocess_exec(
        *cmd,
        env=env or os.environ,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await res.communicate()
    return res.returncode or 0, stdout.decode(), stderr.decode()


async def nix_build(
    uri: str,
    target: str,
    env: dict[str, str] | None = None,
    *args: str,
    nix_file: Path | None = None,
    jobs: int = 1,
    nix_bin: Path = NIX_BIN,
) -> tuple[int, str, str]:
    """Run nix build against a pynixd SSH server."""
    nix_file = nix_file or TEST_NIX
    cmd = [
        str(nix_bin),
        "build",
        "--builders",
        uri,
        "--max-jobs",
        str(jobs),
        "--no-link",
        "--print-out-paths",
        "--file",
        str(nix_file),
        target,
    ]
    if args:
        cmd.extend(args)

    build_env = (env or os.environ).copy()
    build_env["NIX_SSHOPTS"] = (
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    )

    log.debug("Building", cmd=shlex.join(cmd))
    res = await asyncio.create_subprocess_exec(
        *cmd,
        env=build_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await res.communicate()
    return res.returncode or 0, stdout.decode(), stderr.decode()


async def nix_build_store_only(
    uri: str,
    target: str,
    env: dict[str, str] | None = None,
    *args: str,
    nix_file: Path | None = None,
    nix_bin: Path = NIX_BIN,
) -> tuple[int, str, str]:
    """Run nix build --store against a pynixd SSH server."""
    nix_file = nix_file or TEST_NIX
    cmd = [
        str(nix_bin),
        "build",
        "--store",
        uri,
        "--no-link",
        "--print-out-paths",
        "--file",
        str(nix_file),
        target,
    ]
    if args:
        cmd.extend(args)

    build_env = (env or os.environ).copy()
    build_env["NIX_SSHOPTS"] = (
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    )

    log.debug("building_store_only", cmd=shlex.join(cmd))
    res = await asyncio.create_subprocess_exec(
        *cmd,
        env=build_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await res.communicate()
    return res.returncode or 0, stdout.decode(), stderr.decode()


@dataclass
class NixCommandBuilder:
    """StringBuilder-like builder for constructing and running Nix commands."""

    _bin: Path = NIX_BIN
    _command: str = "build"
    _store: str | None = None
    _builders: list[str] = field(default_factory=list)
    _options: list[tuple[str, str]] = field(default_factory=list)
    _file: Path | None = None
    _args: list[str] = field(default_factory=list)
    _env: dict[str, str] = field(default_factory=lambda: os.environ.copy())
    _installables: list[str] = field(default_factory=list)

    def lix(self) -> NixCommandBuilder:
        self._bin = LIX_BIN
        return self

    def nix(self) -> NixCommandBuilder:
        self._bin = NIX_BIN
        return self

    def store(self, uri: str) -> NixCommandBuilder:
        if self._store is not None and self._store != uri:
            log.warning("nix_command_store_overwrite", old=self._store, new=uri)
        self._store = uri
        return self

    def builders(
        self, uri: str, system: str = "", max_jobs: int = 4
    ) -> NixCommandBuilder:
        if " " in uri:
            # Already a full spec (like from Server.builder_uri)
            spec = uri
        else:
            if not system:
                from pynixd.store import get_current_system

                system = get_current_system()
            spec = f"{uri} {system} - {max_jobs}"
        self._builders.append(spec)
        return self

    def file(self, path: str | Path, attribute: str = "") -> NixCommandBuilder:
        path = Path(path)
        if self._file is not None and self._file != path:
            log.warning("nix_command_file_overwrite", old=self._file, new=path)
        self._file = path
        if attribute:
            self._installables.append(attribute)
        return self

    def option(self, name: str, value: str) -> NixCommandBuilder:
        self._options.append((name, value))
        return self

    def arg(self, *args: str) -> NixCommandBuilder:
        self._args.extend(args)
        return self

    def installable(self, *names: str) -> NixCommandBuilder:
        self._installables.extend(names)
        return self

    def remote(self, uri: str) -> NixCommandBuilder:
        self._env["NIX_REMOTE"] = uri
        return self

    def set_env(self, name: str, value: str) -> NixCommandBuilder:
        self._env[name] = value
        return self

    def with_env(self, env: dict[str, str]) -> NixCommandBuilder:
        self._env.update(env)
        return self

    async def run(self) -> tuple[int, str, str]:
        """Execute the constructed nix command."""
        cmd = [str(self._bin), self._command]

        if self._store:
            cmd.extend(["--store", self._store])

        if self._builders:
            # Nix accepts multiple --builders or a comma-separated list.
            # We'll use multiple flags for clarity.
            for b in self._builders:
                cmd.extend(["--builders", b])

        if self._file:
            cmd.extend(["--file", str(self._file)])

        for name, value in self._options:
            cmd.extend(["--option", name, value])

        cmd.extend(self._args)
        cmd.extend(self._installables)

        # Default useful flags for tests
        if self._command == "build":
            if "--no-link" not in cmd:
                cmd.append("--no-link")
            if "--print-out-paths" not in cmd:
                cmd.append("--print-out-paths")

        # Ensure SSH opts are set if not already in env
        if "NIX_SSHOPTS" not in self._env:
            self._env["NIX_SSHOPTS"] = (
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            )

        log.debug("nix_command_run", cmd=shlex.join(cmd))
        res = await asyncio.create_subprocess_exec(
            *cmd,
            env=self._env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await res.communicate()
        rc = res.returncode or 0
        return rc, stdout.decode(), stderr.decode()


def nix_command(bin: Path = NIX_BIN) -> NixCommandBuilder:
    """Entry point for creating a Nix command."""
    return NixCommandBuilder(_bin=bin)


def get_free_port() -> int:
    """Get a free port from the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def make_local_stores(
    n: int = 2,
    *,
    supported_systems: list[str] | None = None,
    prefix: str = "builder",
    max_builds: int = 2,
) -> dict[str, Store]:
    """Create N local socket stores with managed daemons."""
    stores: dict[str, Store] = {}
    for i in range(n):
        store_path = Path(f"/tmp/pynixd-test-{prefix}-{i}")
        os.makedirs(store_path, exist_ok=True)
        store = LocalSocketStore(
            store_path=store_path,
            id=f"{prefix}{i}",
            max_builds=max_builds,
            supported_systems=supported_systems,
            nix_bin=str(NIX_BIN),
        )
        stores[store.id] = store
    return stores


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "nixbuild: tests that require eu.nixbuild.net access"
    )
    config.addinivalue_line(
        "markers", "store: tests using --store (pynixd as eval store)"
    )
    config.addinivalue_line(
        "markers", "builders: tests using --builders (pynixd as remote builder)"
    )
    config.addinivalue_line(
        "markers", "dag: tests building multi-layer DAG derivations"
    )
    config.addinivalue_line("markers", "parallel: build parallelism pressure tests")
    config.addinivalue_line("markers", "matrix: store compatibility matrix tests")


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print benchmark summary table if any benchmark tests ran."""
    _print_bench_summary(terminalreporter, config)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--nix",
        default=TEST_NIX,
        help="Path to test.nix (default: test.nix or PYNIXD_TEST_NIX env)",
    )


@pytest.fixture(scope="session")
def nix_env() -> dict[str, str]:
    """Environment variables for nix subprocess calls."""
    result = os.environ.copy()
    result["NIX_SSHOPTS"] = (
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    )
    return result
