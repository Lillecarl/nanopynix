"""Tests for Content-Addressed (CA) derivations."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import get_free_port, nix_build_store_only, rmtree_robust

from pynixd.instance import PynixdConfig, Server
from pynixd.store import LocalSocketStore, Store

pytestmark = pytest.mark.skip(reason="Temporarily disabled")

CA_NIX = Path("ca.nix").absolute()
# This specific binary is confirmed to support ca-derivations
CA_COMPAT_NIX = Path("/nix/store/ky4l78069kvsy4dcpjf2i4ikjbdvnrdq-nix-2.31.3/bin/nix")


def make_ca_stores(
    n: int = 1,
    *,
    prefix: str = "ca-builder",
) -> dict[str, Store]:
    """Create N local socket stores with CA experimental features enabled."""
    stores: dict[str, Store] = {}
    for i in range(n):
        store_path = Path(f"/tmp/pynixd-test-{prefix}-{i}")
        rmtree_robust(store_path)
        os.makedirs(store_path, exist_ok=True)
        # Add experimental features to the daemon
        extra_args = [
            "--option",
            "experimental-features",
            "nix-command ca-derivations",
        ]
        store = LocalSocketStore(
            store_path=store_path,
            id=f"{prefix}{i}",
            max_builds=2,
            nix_bin=str(CA_COMPAT_NIX),
            extra_args=extra_args,
        )
        store.supported_features.add("ca-derivations")
        stores[store.id] = store
    return stores


@pytest.mark.asyncio
async def test_ca_build(nix_env):
    """Build a CA derivation through pynixd."""
    local_store_path = Path("/tmp/pynixd-test-ca-local")
    rmtree_robust(local_store_path)

    # local_store also needs CA support if it's going to handle CA drvs
    local_store = LocalSocketStore(
        store_path=local_store_path,
        id="local",
        nix_bin=str(CA_COMPAT_NIX),
        extra_args=["--option", "experimental-features", "nix-command ca-derivations"],
    )
    local_store.supported_features.add("ca-derivations")

    builders = make_ca_stores(1)

    config = PynixdConfig(
        local_store=local_store,
        stores=builders,
        ssh_port=get_free_port(),
    )

    async with Server(config) as server:
        # Build the CA derivation
        # We need to pass experimental features to the client nix as well
        build_env = nix_env.copy()
        build_env["NIX_CONFIG"] = "experimental-features = nix-command ca-derivations"

        import conftest

        orig_nix_bin = conftest.NIX_BIN
        conftest.NIX_BIN = CA_COMPAT_NIX
        try:
            rc, stdout, stderr = await nix_build_store_only(
                server.uri(),
                "",  # ca.nix returns the CA derivation directly
                build_env,
                nix_file=CA_NIX,
            )
        finally:
            conftest.NIX_BIN = orig_nix_bin

        assert rc == 0, f"CA build failed:\n{stderr}"

        # In CA builds, the output path is indeterminate until built.
        # Since we confirmed the build succeeded (rc=0), let's find the output.
        # Check if ANY path containing 'ca-test' exists in the local store.
        store_dir = local_store_path / "nix" / "store"
        found_paths = list(store_dir.glob("*-ca-test"))
        assert len(found_paths) > 0, "No ca-test output path found in local store"

        # Pick the newest one if multiple exist
        out_path = max(found_paths, key=lambda p: os.path.getmtime(p))
        with open(out_path) as f:
            assert f.read().strip() == "ca-content"
