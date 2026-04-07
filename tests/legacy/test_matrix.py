"""Store compatibility matrix tests.

Tests all permutations of:
  - Client binary: lix, nix
  - Local store daemon: lix, nix
  - Builder daemon: lix, nix, nixbuild.net
  - Mode: --store, --builders

Each test builds a single simple derivation (test.nix .simple).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from conftest import (
    LIX_BIN,
    NIX_BIN,
    nix_command,
)
from environs import env

from pynixd import Server
from pynixd.instance import NixImplementation
from pynixd.store import (
    LocalSocketStore,
    SSHSubprocessStore,
    Store,
)

log = structlog.get_logger(__name__)


pytestmark = pytest.mark.matrix

# ── Builder factories ─────────────────────────────────────────────────

_counter = 0


def _next_id() -> str:
    global _counter
    _counter += 1
    return str(_counter)


def _local_lix_builder() -> dict[str, Store]:
    n = _next_id()
    store_path = Path(f"/tmp/pynixd-test-matrix-builder-lix-{n}")
    store_path.mkdir(exist_ok=True)
    s = LocalSocketStore(
        store_path=store_path,
        id=f"builder-lix-{n}",
        max_builds=2,
        nix_bin=str(LIX_BIN),
    )
    return {s.id: s}


def _local_nix_builder() -> dict[str, Store]:
    n = _next_id()
    store_path = Path(f"/tmp/pynixd-test-matrix-builder-nix-{n}")
    store_path.mkdir(exist_ok=True)
    s = LocalSocketStore(
        store_path=store_path,
        id=f"builder-nix-{n}",
        max_builds=2,
        nix_bin=str(NIX_BIN),
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


def _local_store(nix_bin: Path) -> LocalSocketStore:
    n = _next_id()
    store_path = Path(f"/tmp/pynixd-test-matrix-local-{n}")
    return LocalSocketStore(
        store_path=store_path,
        id=f"local-{n}",
        max_builds=0,
        max_transfers=64,
        nix_bin=str(nix_bin),
    )


# ── Client helpers (parameterized binary) ─────────────────────────────

CLIENTS = [
    (LIX_BIN, "ssh-ng", NixImplementation.LIX),
    (NIX_BIN, "ssh-ng", NixImplementation.NIX),
]


LOCAL_BINS = [LIX_BIN, NIX_BIN]
LOCAL_BUILDERS = [_local_lix_builder, _local_nix_builder]


# ── Tests: --builders mode (local builders) ───────────────────────────


@pytest.mark.builders
@pytest.mark.timeout(90)
@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("local_bin", LOCAL_BINS)
@pytest.mark.parametrize("builder_factory", LOCAL_BUILDERS)
async def test_builders_local(
    client: tuple[Path, str, NixImplementation],
    local_bin: Path,
    builder_factory,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --builders with local builder."""
    client_bin, uri_fmt, impl = client
    test_nix = request.config.getoption("--nix")
    stores = builder_factory()
    local = _local_store(local_bin)
    client_store = Path(f"/tmp/pynixd-test-matrix-client-{_next_id()}")

    async with Server(stores=stores, local_store=local, ssh_port=0) as server:
        rc, _stdout, stderr = await (
            nix_command(client_bin)
            .store(str(client_store))
            .builders(server.builder_uri(implementation=impl))
            .arg("--max-jobs", "0")
            .file(test_nix, "simple")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0, f"build failed:\n{stderr}"


# ── Tests: --store mode (local builders) ──────────────────────────────


@pytest.mark.store
@pytest.mark.timeout(90)
@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("local_bin", LOCAL_BINS)
@pytest.mark.parametrize("builder_factory", LOCAL_BUILDERS)
async def test_store_local(
    client: tuple[Path, str, NixImplementation],
    local_bin: Path,
    builder_factory,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --store with local builder."""
    client_bin, uri_fmt, impl = client
    test_nix = request.config.getoption("--nix")
    stores = builder_factory()
    local = _local_store(local_bin)

    async with Server(stores=stores, local_store=local, ssh_port=0) as server:
        rc, _stdout, stderr = await (
            nix_command(client_bin)
            .store(server.uri_for(uri_fmt, implementation=impl))
            .file(test_nix, "simple")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0, f"build failed:\n{stderr}"


# ── Tests: --builders mode (nixbuild.net) ─────────────────────────────


@pytest.mark.nixbuild
@pytest.mark.builders
@pytest.mark.timeout(120)
@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("local_bin", LOCAL_BINS)
async def test_builders_nixbuild(
    client: tuple[Path, str, NixImplementation],
    local_bin: Path,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --builders with nixbuild.net."""
    client_bin, uri_fmt, impl = client
    test_nix = request.config.getoption("--nix")
    stores = _nixbuild_builder()
    local = _local_store(local_bin)
    client_store = Path(f"/tmp/pynixd-test-matrix-client-{_next_id()}")

    async with Server(stores=stores, local_store=local, ssh_port=0) as server:
        rc, _stdout, stderr = await (
            nix_command(client_bin)
            .store(str(client_store))
            .builders(server.builder_uri(implementation=impl))
            .arg("--max-jobs", "0")
            .file(test_nix, "simple")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0, f"build failed:\n{stderr}"


# ── Tests: --store mode (nixbuild.net) ────────────────────────────────


@pytest.mark.nixbuild
@pytest.mark.store
@pytest.mark.timeout(120)
@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("local_bin", LOCAL_BINS)
async def test_store_nixbuild(
    client: tuple[Path, str, NixImplementation],
    local_bin: Path,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --store with nixbuild.net."""
    client_bin, uri_fmt, impl = client
    test_nix = request.config.getoption("--nix")
    stores = _nixbuild_builder()
    local = _local_store(local_bin)

    async with Server(stores=stores, local_store=local, ssh_port=0) as server:
        rc, _stdout, stderr = await (
            nix_command(client_bin)
            .store(server.uri_for(uri_fmt, implementation=impl))
            .file(test_nix, "simple")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0, f"build failed:\n{stderr}"
