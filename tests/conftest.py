"""Integration test helpers for pynixd."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import socket
import subprocess
from pathlib import Path

import pytest
from environs import Env

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

env = Env()

TEST_NIX = env.path("PYNIXD_TEST_NIX", Path("test.nix"))

# LIX_BIN / NIX_BIN: paths to lix and nix binaries for LocalSubprocessStore.
# Default to "nix" if neither is set.
LIX_BIN = env.path("LIX_BIN", Path("nix"))
NIX_BIN = env.path("NIX_BIN", Path("nix"))


@pytest.fixture(scope="session", autouse=True)
def cleanup_bench_paths():
    """Ensure large benchmark artifacts are deleted before tests run."""
    log.info("Cleaning up old benchmark paths...")
    try:
        # The `-S` flag to path-info gives us the size, but we don't use it here.
        # We just need the paths.
        result = subprocess.run(
            f"{NIX_BIN} path-info -rS /nix/store | grep bench-100mb | cut -f1",
            shell=True,
            capture_output=True,
            text=True,
        )
        for p in result.stdout.splitlines():
            if p.strip():
                log.info("Deleting old benchmark path: %s", p)
                subprocess.run(
                    [str(NIX_BIN), "store", "delete", p], capture_output=True
                )
    except Exception as e:
        log.warning("Could not cleanup benchmark paths: %s", e)


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
) -> tuple[int, str, str]:
    """Run nix build against a pynixd SSH server."""
    nix_file = nix_file or TEST_NIX
    cmd = [
        str(NIX_BIN),
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
    # Add any extra args (like --file if the caller passed it as positional)
    if args:
        cmd.extend(args)

    build_env = (env or os.environ).copy()
    build_env["NIX_SSHOPTS"] = (
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    )

    log.debug("Building: %s", shlex.join(cmd))
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
) -> tuple[int, str, str]:
    """Run nix build --store against a pynixd SSH server."""
    nix_file = nix_file or TEST_NIX
    cmd = [
        str(NIX_BIN),
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

    log.debug("Building (store only): %s", shlex.join(cmd))
    res = await asyncio.create_subprocess_exec(
        *cmd,
        env=build_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await res.communicate()
    return res.returncode or 0, stdout.decode(), stderr.decode()


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
                # Print header (first 7 lines) + top 40 lines (most indented)
                terminalreporter.write_line("    Profile summary:")
                for line in lines[:7]:
                    terminalreporter.write_line(f"    {line.rstrip()}")
                # Find the most detailed stack traces (most indented)
                # These appear later in the profile
                stack_lines = [line for line in lines[7:] if line.startswith("   │")]
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
