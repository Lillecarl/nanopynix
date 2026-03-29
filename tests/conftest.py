"""Integration test helpers for pynixd."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from pynixd.ssh_server import run_server as run_ssh_server
from pynixd.store import LocalSocketStore, Store

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
# Silence high-frequency per-op loggers while keeping the rest at DEBUG.
# Use pynixd.op.{OpName} to tune individual ops.
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

log = logging.getLogger(__name__)

TEST_NIX = os.environ.get("PYNIXD_TEST_NIX", "test.nix")

# LIX_BIN / NIX_BIN: paths to lix and nix binaries for LocalSubprocessStore.
# Default to "nix" if neither is set.
LIX_BIN = os.environ.get("LIX_BIN", "lix")
NIX_BIN = os.environ.get("NIX_BIN", "nix")


def get_current_system() -> str:
    """Get the current system via nix."""
    result = subprocess.run(
        [
            NIX_BIN,
            "eval",
            "--store",
            "dummy://",
            "--impure",
            "--raw",
            "--expr",
            "builtins.currentSystem",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


CURRENT_SYSTEM = None


def _get_system() -> str:
    global CURRENT_SYSTEM
    if CURRENT_SYSTEM is None:
        CURRENT_SYSTEM = get_current_system()
    return CURRENT_SYSTEM


@dataclass
class PynixdServer:
    """A running pynixd server with its stores and client store."""

    host: str
    port: int
    username: str
    stores: dict[str, Store]
    client_store_path: str
    system: str
    njobs: int
    _task: asyncio.Task = field(default=None, repr=False)

    @property
    def uri(self) -> str:
        """Default URI using nix-style host:port."""
        return self.uri_for(NIX_BIN)

    def uri_for(self, uri_format: str = "nix") -> str:
        """Generate ssh-ng:// URI compatible with the given client.

        uri_format="lix": ssh-ng://user@host?port=N
        uri_format="nix": ssh-ng://user@host:port
        """
        if uri_format == "lix":
            return f"ssh-ng://{self.username}@{self.host}?port={self.port}"
        return f"ssh-ng://{self.username}@{self.host}:{self.port}"

    def builder_uri(
        self,
        *,
        uri_format: str = "nix",
        system: str | None = None,
        max_jobs: int | None = None,
        speed_factor: int = 1,
        features: str = "-",
    ) -> str:
        """Build a --builders URI string with optional overrides.

        Format: "URI system features max_jobs speed_factor"
        """
        return (
            f"{self.uri_for(uri_format)} "
            f"{system or self.system} "
            f"{features} "
            f"{max_jobs if max_jobs is not None else self.njobs} "
            f"{speed_factor}"
        )

    async def close(self) -> None:
        """Stop the server and close all stores."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for store in self.stores.values():
            await store.close()
        await self._local_store.close()

    # Stores are assigned during __await__ - store ref for close()
    _local_store: Store = field(default=None, repr=False)


@asynccontextmanager
async def run_pynixd(
    stores: dict[str, Store],
    *,
    local_store: Store | None = None,
    client_store_path: str = "/tmp/pynixd-test-client",
    njobs: int = 4,
) -> AsyncIterator[PynixdServer]:
    """Start a pynixd SSH server with the given stores.

    Usage:
        async with run_pynixd(stores) as server:
            uri = server.uri                # bare ssh-ng:// URI
            builder = server.builder_uri()  # "URI system - njobs 1" for --builders
            ...
        # server automatically stopped

    Args:
        stores: Dict of store_id -> Store for build workers
        local_store: Store for pynixd's local store (default: local subprocess)
        client_store_path: Local store path for the nix client
        njobs: Max concurrent jobs for --builders (default 4)
    """
    if local_store is None:
        local_store = LocalSocketStore(
            store_path="/tmp/pynixd-test-local",
            id="local",
            max_builds=0,
            max_transfers=64,
            nix_bin=NIX_BIN,
        )

    os.makedirs(client_store_path, exist_ok=True)

    ready = asyncio.Event()
    port_holder: list[int] = []

    async def _run() -> None:
        await run_ssh_server(
            stores=stores,
            local_store=local_store,
            host="127.0.0.1",
            port=0,
            ready_event=ready,
            port_holder=port_holder,
        )

    task = asyncio.create_task(_run())

    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
    except TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise RuntimeError("Could not start pynixd SSH server")

    bound_port = port_holder[0]
    username = os.environ.get("USER", "root")

    server = PynixdServer(
        host="127.0.0.1",
        port=bound_port,
        username=username,
        stores=stores,
        client_store_path=client_store_path,
        system=_get_system(),
        njobs=njobs,
        _task=task,
        _local_store=local_store,
    )
    yield server
    await server.close()


# ── Helper functions for common server configurations ──────────────────────────


def make_local_stores(
    n: int = 2,
    *,
    supported_systems: list[str] | None = None,
    prefix: str = "builder",
    max_builds: int = 2,
) -> dict[str, Store]:
    """Create N local socket stores with managed daemons.

    Args:
        n: Number of stores to create
        supported_systems: If set, all stores only support these systems
        prefix: ID prefix for stores
        max_builds: Max concurrent builds per store
    """
    stores: dict[str, Store] = {}
    for i in range(n):
        store_path = f"/tmp/pynixd-test-{prefix}-{i}"
        os.makedirs(store_path, exist_ok=True)
        store = LocalSocketStore(
            store_path=store_path,
            id=f"{prefix}{i}",
            max_builds=max_builds,
            supported_systems=supported_systems,
            nix_bin=NIX_BIN,
        )
        stores[store.id] = store
    return stores


# ── Pytest helpers ────────────────────────────────────────────────────────────


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
    config.addinivalue_line(
        "markers", "benchmark: NAR streaming performance benchmarks"
    )
    config.addinivalue_line("markers", "parallel: build parallelism pressure tests")
    config.addinivalue_line("markers", "matrix: store compatibility matrix tests")


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print benchmark summary tables if any benchmark tests ran."""
    _print_nar_bench_summary(terminalreporter, config)
    _print_pynixd_bench_summary(terminalreporter, config)
    _print_build_bench_summary(terminalreporter, config)


def _print_nar_bench_summary(
    terminalreporter: pytest.TerminalReporter,
    config: pytest.Config,
) -> None:
    from test_bench_nar import BenchResult, _bench_results_key

    results: list[BenchResult] = config.stash.get(_bench_results_key, [])
    if not results:
        return

    terminalreporter.section("NAR Benchmark Summary")

    # Chunk-parameterized results as a matrix
    chunk_sizes = sorted(set(r.chunk_kb for r in results if r.chunk_kb > 0))
    labels = list(dict.fromkeys(r.label for r in results))

    if chunk_sizes:
        header = f"{'Test':<28s}"
        for ck in chunk_sizes:
            header += f"  {ck:>7d}KB"
        terminalreporter.write_line(header)
        terminalreporter.write_line("-" * len(header))

        for label in labels:
            row_results = {r.chunk_kb: r for r in results if r.label == label}
            if not any(ck in row_results for ck in chunk_sizes):
                continue
            row = f"{label:<28s}"
            for ck in chunk_sizes:
                r = row_results.get(ck)
                if r:
                    row += f"  {r.mb_per_s:>7.1f}  "
                else:
                    row += f"  {'—':>7s}  "
            terminalreporter.write_line(row)

    # Non-chunked results (serving benchmarks)
    non_chunked = [r for r in results if r.chunk_kb == 0]
    if non_chunked:
        terminalreporter.write_line("")
        for r in non_chunked:
            terminalreporter.write_line(
                f"{r.label:<28s}  {r.mb_per_s:.1f} MB/s, {r.paths_per_s:.0f} paths/s"
            )

    terminalreporter.write_line("")
    terminalreporter.write_line("Values are MB/s (higher is better)")


def _print_pynixd_bench_summary(
    terminalreporter: pytest.TerminalReporter,
    config: pytest.Config,
) -> None:
    from test_bench_pynixd import PynixdBenchResult, _pynixd_bench_key

    results: list[PynixdBenchResult] = config.stash.get(_pynixd_bench_key, [])
    if not results:
        return

    terminalreporter.section("pynixd Benchmark Summary")

    for r in results:
        parts = [f"{r.label:<36s}  {r.ops_per_s:>8.0f} ops/s"]
        if r.total_bytes > 0:
            parts.append(f"  {r.mb_per_s:>7.1f} MB/s")
        parts.append(f"  ({r.elapsed:.1f}s)")
        terminalreporter.write_line("".join(parts))


def _print_build_bench_summary(
    terminalreporter: pytest.TerminalReporter,
    config: pytest.Config,
) -> None:
    from test_bench_build_unix import BenchResult, _build_bench_key

    results: list[BenchResult] = config.stash.get(_build_bench_key, [])
    if not results:
        return

    terminalreporter.section("Build Benchmark Summary")

    for r in results:
        terminalreporter.write_line(
            f"  {r.label:<36s}  {r.count:>4d} drv  ({r.elapsed:.1f}s)"
        )
        if r.profile_path:
            try:
                with open(r.profile_path) as f:
                    lines = f.readlines()
                # Print header (first 7 lines) + top 40 lines (most indented call stacks)
                terminalreporter.write_line("    Profile summary:")
                for line in lines[:7]:
                    terminalreporter.write_line(f"    {line.rstrip()}")
                # Find the most detailed stack traces (most indented)
                # These appear later in the profile
                stack_lines = [l for l in lines[7:] if l.startswith("   │")]
                for line in stack_lines[:40]:
                    terminalreporter.write_line(f"    {line.rstrip()}")
                terminalreporter.write_line(f"    (full profile: {r.profile_path})")
            except Exception as e:
                terminalreporter.write_line(f"    (Could not read profile: {e})")
    terminalreporter.write_line("")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--nix",
        default=TEST_NIX,
        help="Path to test.nix (default: test.nix or PYNIXD_TEST_NIX env)",
    )


@pytest.fixture(scope="session")
def nix_env() -> dict[str, str]:
    """Environment variables for nix subprocess calls."""
    env = os.environ.copy()
    env["NIX_SSHOPTS"] = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    return env


# ── Subprocess helpers ────────────────────────────────────────────────────────


def _sync_kill_process_group(pid: int) -> None:
    """Kill a process group synchronously."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def _run_subprocess_with_timeout(
    cmd: list[str],
    env: dict[str, str],
    timeout: int,
) -> tuple[int, str, str]:
    """Run a subprocess with timeout.

    Returns (returncode, stdout, stderr).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )

    try:
        stdout_data, stderr_data = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        _sync_kill_process_group(proc.pid)
        pytest.fail(f"subprocess timed out after {timeout}s")
    except asyncio.CancelledError:
        _sync_kill_process_group(proc.pid)
        raise

    return proc.returncode, stdout_data.decode(), stderr_data.decode()


async def nix_build(
    builder_uri: str,
    client_store_path: str,
    env: dict[str, str],
    *extra_args: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run a nix build via --builders flag."""
    cmd = [
        NIX_BIN,
        "build",
        "--store",
        client_store_path,
        "--builders",
        builder_uri,
        "--max-jobs",
        "0",
        "--no-link",
        *extra_args,
    ]
    log.info("nix_build builder_uri=%r", builder_uri)
    log.info("nix_build cmd=%s", " ".join(shlex.quote(a) for a in cmd))

    returncode, stdout_s, stderr_s = await _run_subprocess_with_timeout(
        cmd, env, timeout
    )

    log.info(
        "nix_build rc=%d\n  stdout: %s\n  stderr: %s",
        returncode,
        stdout_s[:500],
        stderr_s[:2000],
    )
    return returncode, stdout_s, stderr_s


async def nix_build_store_only(
    store_uri: str,
    env: dict[str, str],
    *extra_args: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run a nix build using pynixd as the store directly."""
    cmd = [
        NIX_BIN,
        "build",
        "--store",
        store_uri,
        "--no-link",
        *extra_args,
    ]
    log.info("nix_build_store_only: %s", " ".join(shlex.quote(a) for a in cmd))

    returncode, stdout_s, stderr_s = await _run_subprocess_with_timeout(
        cmd, env, timeout
    )

    log.info(
        "nix_build_store_only rc=%d\n  stdout: %s\n  stderr: %s",
        returncode,
        stdout_s[:500],
        stderr_s[:2000],
    )
    return returncode, stdout_s, stderr_s
