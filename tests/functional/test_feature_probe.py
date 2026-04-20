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

    await store.probe()

    fm = store.feature_matrix
    assert fm, "feature_matrix should not be empty after probe"
    assert "x86_64-linux" in fm
    assert "big-parallel" in fm["x86_64-linux"]
    assert "ca-derivations" in fm["x86_64-linux"]
    assert "nixos-test" in fm["x86_64-linux"]
    assert "apple-virt" not in fm.get("x86_64-linux", set())
    assert "uid-range" not in fm.get("x86_64-linux", set())


@pytest.mark.timeout(120)
@pytest.mark.nixbuild
@pytest.mark.skip(reason="requires nixbuild.net; pass -m nixbuild to run")
async def test_feature_probe_nixbuild_net() -> None:
    store = SSHSubprocessStore(
        host="eu.nixbuild.net",
        id="nixbuild-net",
        username="lillecarl",
        client_keys=[Path("~/.ssh/id_ed25519")],
        max_builds=10,
        max_transfers=10,
    )

    await store.probe()

    fm = store.feature_matrix
    assert fm, "feature_matrix should not be empty after probe"
    assert "x86_64-linux" in fm
    assert "benchmark" in fm["x86_64-linux"]
    assert "big-parallel" in fm["x86_64-linux"]
    assert "nixos-test" in fm["x86_64-linux"]
