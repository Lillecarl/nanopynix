"""Store compatibility matrix tests.

Tests all permutations of:
  - Client binary: lix, nix
  - Local store daemon: lix, nix
  - Builder daemon: lix, nix, nixbuild.net
  - Mode: --store, --builders

Each test builds a single simple derivation (test.nix .simple).
"""

from __future__ import annotations

import logging
import os
import shlex

import pytest
from conftest import (
    LIX_BIN,
    NIX_BIN,
    _run_subprocess_with_timeout,
    run_pynixd,
)
from environs import Env

from pynixd.store import (
    LocalSocketStore,
    SSHSubprocessStore,
    Store,
)

log = logging.getLogger(__name__)

env = Env()

pytestmark = pytest.mark.matrix

# ── Builder factories ─────────────────────────────────────────────────

_counter = 0


def _next_id() -> int:
    global _counter
    _counter += 1
    return _counter


def _local_lix_builder() -> dict[str, Store]:
    n = _next_id()
    store_path = f"/tmp/pynixd-test-matrix-builder-lix-{n}"
    os.makedirs(store_path, exist_ok=True)
    s = LocalSocketStore(
        store_path=store_path,
        id=f"builder-lix-{n}",
        max_builds=2,
        nix_bin=LIX_BIN,
    )
    return {s.id: s}


def _local_nix_builder() -> dict[str, Store]:
    n = _next_id()
    store_path = f"/tmp/pynixd-test-matrix-builder-nix-{n}"
    os.makedirs(store_path, exist_ok=True)
    s = LocalSocketStore(
        store_path=store_path,
        id=f"builder-nix-{n}",
        max_builds=2,
        nix_bin=NIX_BIN,
    )
    return {s.id: s}


def _nixbuild_builder() -> dict[str, Store]:
    username = env.str("USER", "root")
    s = SSHSubprocessStore(
        host="eu.nixbuild.net",
        username=username,
        id="nixbuild",
        max_builds=2,
    )
    return {s.id: s}


def _local_store(nix_bin: str) -> LocalSocketStore:
    n = _next_id()
    store_path = f"/tmp/pynixd-test-matrix-local-{n}"
    return LocalSocketStore(
        store_path=store_path,
        id=f"local-{n}",
        max_builds=0,
        max_transfers=64,
        nix_bin=nix_bin,
    )


# ── Client helpers (parameterized binary) ─────────────────────────────


async def _nix_build_builders(
    client_bin: str,
    builder_uri: str,
    client_store_path: str,
    env: dict[str, str],
    *extra_args: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    os.makedirs(client_store_path, exist_ok=True)
    cmd = [
        client_bin,
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
    log.info("matrix build (--builders): %s", " ".join(shlex.quote(a) for a in cmd))
    rc, out, err = await _run_subprocess_with_timeout(cmd, env, timeout)
    log.info("rc=%d\n  stdout: %s\n  stderr: %s", rc, out[:500], err[:2000])
    return rc, out, err


async def _nix_build_store(
    client_bin: str,
    store_uri: str,
    env: dict[str, str],
    *extra_args: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    cmd = [
        client_bin,
        "build",
        "--store",
        store_uri,
        "--no-link",
        *extra_args,
    ]
    log.info("matrix build (--store): %s", " ".join(shlex.quote(a) for a in cmd))
    rc, out, err = await _run_subprocess_with_timeout(cmd, env, timeout)
    log.info("rc=%d\n  stdout: %s\n  stderr: %s", rc, out[:500], err[:2000])
    return rc, out, err


# ── Matrix parameters ─────────────────────────────────────────────────

CLIENTS = [
    pytest.param((LIX_BIN, "lix"), id="lix-client"),
    pytest.param((NIX_BIN, "nix"), id="nix-client"),
]

LOCAL_BINS = [
    pytest.param(LIX_BIN, id="lix-store"),
    pytest.param(NIX_BIN, id="nix-store"),
]

LOCAL_BUILDERS = [
    pytest.param(_local_lix_builder, id="lix-builder"),
    pytest.param(_local_nix_builder, id="nix-builder"),
]

NIXBUILD_BUILDERS = [
    pytest.param(_nixbuild_builder, id="nixbuild-builder"),
]

ALL_BUILDERS = LOCAL_BUILDERS + NIXBUILD_BUILDERS


# ── Tests: --builders mode (local builders) ───────────────────────────


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(90)
@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("local_bin", LOCAL_BINS)
@pytest.mark.parametrize("builder_factory", LOCAL_BUILDERS)
async def test_builders_local(
    client: tuple[str, str],
    local_bin: str,
    builder_factory,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --builders with local builder."""
    client_bin, uri_fmt = client
    test_nix = request.config.getoption("--nix")
    stores = builder_factory()
    local = _local_store(local_bin)
    client_store = f"/tmp/pynixd-test-matrix-client-{_next_id()}"

    async with run_pynixd(
        stores, local_store=local, client_store_path=client_store
    ) as server:
        rc, stdout, stderr = await _nix_build_builders(
            client_bin,
            server.builder_uri(uri_format=uri_fmt),
            server.client_store_path,
            nix_env,
            "--file",
            test_nix,
            "simple",
        )
        assert rc == 0, f"build failed:\n{stderr}"


# ── Tests: --store mode (local builders) ──────────────────────────────


@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(90)
@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("local_bin", LOCAL_BINS)
@pytest.mark.parametrize("builder_factory", LOCAL_BUILDERS)
async def test_store_local(
    client: tuple[str, str],
    local_bin: str,
    builder_factory,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --store with local builder."""
    client_bin, uri_fmt = client
    test_nix = request.config.getoption("--nix")
    stores = builder_factory()
    local = _local_store(local_bin)

    async with run_pynixd(stores, local_store=local) as server:
        rc, stdout, stderr = await _nix_build_store(
            client_bin,
            server.uri_for(uri_fmt),
            nix_env,
            "--file",
            test_nix,
            "simple",
        )
        assert rc == 0, f"build failed:\n{stderr}"


# ── Tests: --builders mode (nixbuild.net) ─────────────────────────────


@pytest.mark.nixbuild
@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(120)
@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("local_bin", LOCAL_BINS)
async def test_builders_nixbuild(
    client: tuple[str, str],
    local_bin: str,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --builders with nixbuild.net."""
    client_bin, uri_fmt = client
    test_nix = request.config.getoption("--nix")
    stores = _nixbuild_builder()
    local = _local_store(local_bin)
    client_store = f"/tmp/pynixd-test-matrix-client-{_next_id()}"

    async with run_pynixd(
        stores, local_store=local, client_store_path=client_store
    ) as server:
        rc, stdout, stderr = await _nix_build_builders(
            client_bin,
            server.builder_uri(uri_format=uri_fmt),
            server.client_store_path,
            nix_env,
            "--file",
            test_nix,
            "simple",
        )
        assert rc == 0, f"build failed:\n{stderr}"


# ── Tests: --store mode (nixbuild.net) ────────────────────────────────


@pytest.mark.nixbuild
@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(120)
@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("local_bin", LOCAL_BINS)
async def test_store_nixbuild(
    client: tuple[str, str],
    local_bin: str,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --store with nixbuild.net."""
    client_bin, uri_fmt = client
    test_nix = request.config.getoption("--nix")
    stores = _nixbuild_builder()
    local = _local_store(local_bin)

    async with run_pynixd(stores, local_store=local) as server:
        rc, stdout, stderr = await _nix_build_store(
            client_bin,
            server.uri_for(uri_fmt),
            nix_env,
            "--file",
            test_nix,
            "simple",
        )
        assert rc == 0, f"build failed:\n{stderr}"
