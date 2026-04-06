"""Tests for Unix domain socket server."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import structlog
from conftest import (
    LIX_BIN,
    NIX_BIN,
    nix_command,
    nix_store,
)

from pynixd.instance import PynixdConfig, Server
from pynixd.store import LocalSocketStore, Store

log = structlog.get_logger(__name__)


async def _nix_build_unix(
    socket_path: Path,
    env: dict[str, str],
    *extra_args: str,
) -> tuple[int, str, str]:
    """Run nix build using pynixd unix socket as the store."""
    return await (
        nix_command(LIX_BIN)
        .remote(f"unix://{socket_path}")
        .arg(*extra_args)
        .with_env(env)
        .run()
    )


async def _nix_build_direct(
    store_path: Path,
    env: dict[str, str],
    *extra_args: str,
) -> tuple[int, str, str]:
    """Run nix build directly against a local store (no pynixd).."""
    return await (
        nix_command(LIX_BIN)
        .store(str(store_path))
        .arg("--max-jobs", "150")
        .arg(*extra_args)
        .with_env(env)
        .run()
    )


async def _nix_store_unix(
    socket_path: Path,
    env: dict[str, str],
    subcommand: str,
    *args: str,
) -> tuple[int, str, str]:
    """Run nix store subcommand against pynixd unix socket."""
    return await (
        nix_store(LIX_BIN)
        .arg(subcommand)
        .arg(*args)
        .remote(f"unix://{socket_path}")
        .with_env(env)
        .run()
    )


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
        rc, _stdout, stderr = await _nix_build_unix(
            socket_path,
            nix_env,
            "--file",
            str(test_nix),
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
        rc, _stdout, stderr = await _nix_build_unix(
            socket_path,
            nix_env,
            "--file",
            str(test_nix),
            "simple",
        )
        assert rc == 0

        # Now query info
        # Get path using our builder
        rc, stdout, _ = await (
            nix_command(LIX_BIN)
            .file(test_nix, "simple")
            .arg("--print-out-paths")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0
        path = stdout.strip()

        rc, stdout, stderr = await _nix_store_unix(
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
        rc, stdout, stderr = await _nix_store_unix(
            socket_path,
            nix_env,
            "gc",
        )
        assert rc == 0
