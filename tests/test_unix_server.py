"""Tests for Unix domain socket server."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import structlog
from conftest import (
    NIX_BIN,
    _run_subprocess_with_timeout,
)

from pynixd.instance import PynixdConfig, Server
from pynixd.store import LocalSocketStore, Store

log = structlog.get_logger(__name__)


def _nix_build_unix(
    socket_path: Path,
    env: dict[str, str],
    *extra_args: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run nix build using pynixd unix socket as the store."""
    store_uri = f"unix://{socket_path}"
    cmd = [
        str(NIX_BIN),
        "build",
        "--store",
        store_uri,
        "--no-link",
        *extra_args,
    ]
    log.info("nix_build_unix: %s", " ".join(shlex.quote(a) for a in cmd))
    return _run_subprocess_with_timeout(cmd, env, timeout)


def _nix_build_direct(
    store_path: Path,
    env: dict[str, str],
    *extra_args: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run nix build directly against a local store (no pynixd)."""
    cmd = [
        str(NIX_BIN),
        "build",
        "--store",
        str(store_path),
        "--no-link",
        "--max-jobs",
        "150",
        *extra_args,
    ]
    log.info("nix_build_direct: %s", " ".join(shlex.quote(str(a)) for a in cmd))
    return _run_subprocess_with_timeout(cmd, env, timeout)


def _nix_store_unix(
    socket_path: Path,
    env: dict[str, str],
    *args: str,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run nix store subcommand against pynixd unix socket."""
    store_uri = f"unix://{socket_path}"
    cmd = [
        str(NIX_BIN),
        "store",
        *args,
        "--store",
        store_uri,
    ]
    log.info("nix_store_unix: %s", " ".join(shlex.quote(a) for a in cmd))
    return _run_subprocess_with_timeout(cmd, env, timeout)


@pytest.fixture
def local_store() -> LocalSocketStore:
    """Local store for pynixd."""
    store_path = Path("/tmp/pynixd-test-unix-local")
    store_path.mkdir(exist_ok=True)
    return LocalSocketStore(
        store_path=store_path,
        id="local",
        max_builds=0,
        max_transfers=64,
        nix_bin=str(NIX_BIN),
    )


@pytest.fixture
def builder_store() -> LocalSocketStore:
    """Builder store for pynixd."""
    store_path = Path("/tmp/pynixd-test-unix-builder")
    store_path.mkdir(exist_ok=True)
    return LocalSocketStore(
        store_path=store_path,
        id="builder",
        max_builds=2,
        max_transfers=4,
        nix_bin=str(NIX_BIN),
    )


@asynccontextmanager
async def run_unix_server(
    local_store: Store,
    stores: dict[str, Store],
) -> AsyncIterator[Path]:
    """Start pynixd with a Unix socket server."""
    socket_path = Path("/tmp/pynixd-test.socket")
    if socket_path.exists():
        socket_path.unlink()

    config = PynixdConfig(
        local_store=local_store,
        stores=stores,
        unix_path=socket_path,
    )

    server = Server(config)
    await server.start()

    try:
        yield socket_path
    finally:
        await server.close()
        await server.wait_finished()
        if os.path.exists(socket_path):
            os.remove(socket_path)


async def test_unix_build(
    local_store: Store,
    builder_store: Store,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via Unix socket."""
    test_nix = request.config.getoption("--nix")
    stores = {builder_store.id: builder_store}

    async with run_unix_server(local_store, stores) as socket_path:
        rc, _stdout, stderr = _nix_build_unix(
            socket_path,
            nix_env,
            "--file",
            test_nix,
            "simple",
        )
        assert rc == 0, f"Unix build failed:\n{stderr}"


async def test_unix_store_info(
    local_store: Store,
    builder_store: Store,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Query path info via Unix socket."""
    test_nix = request.config.getoption("--nix")
    stores = {builder_store.id: builder_store}

    async with run_unix_server(local_store, stores) as socket_path:
        # First build it to make sure it exists
        rc, _stdout, stderr = _nix_build_unix(
            socket_path,
            nix_env,
            "--file",
            test_nix,
            "simple",
        )
        assert rc == 0

        # Now query info
        cmd = ["nix", "path-info", "--file", str(test_nix), "simple"]
        path = subprocess.check_output(cmd).decode().strip()

        rc, stdout, stderr = _nix_store_unix(
            socket_path,
            nix_env,
            "path-info",
            path,
        )
        assert rc == 0
        assert path in stdout


async def test_unix_gc(
    local_store: Store,
    builder_store: Store,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Run GC via Unix socket."""
    stores = {builder_store.id: builder_store}

    async with run_unix_server(local_store, stores) as socket_path:
        rc, stdout, stderr = _nix_store_unix(
            socket_path,
            nix_env,
            "gc",
        )
        assert rc == 0
