"""Tests using eu.nixbuild.net as the backend."""

from __future__ import annotations

import os

import pytest
from conftest import (
    nix_build,
    nix_build_store_only,
    run_pynixd,
)
from environs import Env

from pynixd.store import SSHSubprocessStore

pytestmark = pytest.mark.nixbuild

env = Env()


def _nixbuild_stores() -> dict[str, SSHSubprocessStore]:
    username = env.str("USER", "root")
    store = SSHSubprocessStore(
        host="eu.nixbuild.net",
        username=username,
        id="nixbuild",
        max_builds=2,
    )
    return {store.id: store}


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_simple_builders(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --builders through nixbuild."""
    test_nix = request.config.getoption("--nix")
    stores = _nixbuild_stores()

    async with run_pynixd(stores) as server:
        returncode, stdout, stderr = await nix_build(
            server.builder_uri(),
            server.client_store_path,
            nix_env,
            "--file",
            test_nix,
            "simple",
        )
        assert returncode == 0, f"nix build failed:\n{stderr}"


@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_simple_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --store through nixbuild."""
    test_nix = request.config.getoption("--nix")
    stores = _nixbuild_stores()

    async with run_pynixd(stores) as server:
        returncode, stdout, stderr = await nix_build_store_only(
            server.uri,
            nix_env,
            "--file",
            test_nix,
            "simple",
        )
        assert returncode == 0, f"nix build --store failed:\n{stderr}"


@pytest.mark.builders
@pytest.mark.dag
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_dag_builders(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .dag via --builders through nixbuild."""
    test_nix = request.config.getoption("--nix")
    stores = _nixbuild_stores()

    async with run_pynixd(stores) as server:
        returncode, stdout, stderr = await nix_build(
            server.builder_uri(),
            server.client_store_path,
            nix_env,
            "--file",
            test_nix,
            "dag",
        )
        assert returncode == 0, f"DAG build failed:\n{stderr}"


@pytest.mark.store
@pytest.mark.dag
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_dag_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .dag via --store through nixbuild."""
    test_nix = request.config.getoption("--nix")
    stores = _nixbuild_stores()

    async with run_pynixd(stores) as server:
        returncode, stdout, stderr = await nix_build_store_only(
            server.uri,
            nix_env,
            "--file",
            test_nix,
            "dag",
        )
        assert returncode == 0, f"DAG build --store failed:\n{stderr}"
