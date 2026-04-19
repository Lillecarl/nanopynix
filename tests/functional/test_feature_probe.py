"""Feature probe: send BuildDerivationRequest with requiredSystemFeatures
directly to a store, constructing derivations entirely in-memory.

No .drv files on disk needed — the daemon uses the wire-provided BasicDerivation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from pynixd.store import LocalSocketStore, SSHSubprocessStore
from tests.conftest import (
    STORE_PREFIX,
    get_test_store_kwargs,
    rmtree_robust,
)
from tests.nix_config import NixConfig

log = structlog.get_logger(__name__)

FEATURE_NIX_CONFIG = NixConfig.for_ca_derivations(
    substituters=(
        "https://cache.nixos.org/",
        "unix:///nix/var/nix/daemon-socket/socket?root=/",
    ),
)


@pytest.mark.timeout(60)
async def test_feature_probe_in_memory() -> None:
    store_path = STORE_PREFIX / "feature-probe"
    rmtree_robust(store_path)
    kwargs = get_test_store_kwargs(nix_config=FEATURE_NIX_CONFIG)
    store: LocalSocketStore | SSHSubprocessStore = LocalSocketStore(
        id="feature-probe",
        store_path=store_path,
        max_builds=10,
        max_transfers=10,
        **kwargs,
    )
    await store.ensure_daemon()

    await store.probe_systems()
    await store.probe_features()

    assert "x86_64-linux" in store.systems
    assert "big-parallel" in store.system_features
    assert "ca-derivations" in store.system_features
    assert "nixos-test" in store.system_features
    assert "apple-virt" not in store.system_features
    assert "uid-range" not in store.system_features


@pytest.mark.timeout(120)
async def test_feature_probe_nixbuild_net() -> None:
    store = SSHSubprocessStore(
        host="eu.nixbuild.net",
        id="nixbuild-net",
        username="lillecarl",
        client_keys=[Path("~/.ssh/id_ed25519")],
        max_builds=10,
        max_transfers=10,
    )

    await store.probe_systems()
    await store.probe_features()

    assert "x86_64-linux" in store.systems
    assert "benchmark" in store.system_features
    assert "big-parallel" in store.system_features
    assert "nixos-test" in store.system_features
