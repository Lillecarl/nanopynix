"""Feature probe: send BuildDerivationRequest with requiredSystemFeatures
directly to a store, constructing derivations entirely in-memory.

No .drv files on disk needed — the daemon uses the wire-provided BasicDerivation.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform

import pytest
import structlog

from pathlib import Path
from pynixd.operations.base import BasicDerivation, BuildMode, DerivationOutput
from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.store import LocalSocketStore, SSHSubprocessStore
from pynixd.store_path import StorePath
from pynixd.system_features import KNOWN_FEATURES
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

NIX32 = "0123456789abcdfghijklmnpqrsvwxyz"


def random_nix32_hash() -> str:
    """Generate a random 32-char nix base32 hash for use as a fake drv hash."""
    digest = hashlib.sha256(os.urandom(32)).digest()
    buf = int.from_bytes(digest, "big")
    chars = []
    for _ in range(32):
        chars.append(NIX32[buf & 0x1F])
        buf >>= 5
    return "".join(reversed(chars))


def make_probe_derivation(feature: str) -> tuple[StorePath, BasicDerivation]:
    """Construct a minimal BasicDerivation that requires a system feature.

    The drv_path is fake but valid-format — the daemon doesn't read .drv from
    disk for BuildDerivation, it uses the wire-provided BasicDerivation.
    """
    system = f"{platform.machine()}-{platform.system().lower()}"
    drv_name = f"probe-{feature}"
    drv_hash = random_nix32_hash()
    drv_path = StorePath(f"/nix/store/{drv_hash}-{drv_name}.drv")
    out_path = f"/nix/store/{drv_hash}-{drv_name}"

    if feature == "kvm":
        args = [
            "-c",
            "test -w /dev/kvm && echo kvm > $out || { echo 'kvm: /dev/kvm not writable' >&2; exit 1; }",
        ]
    else:
        args = ["-c", f"echo {feature} > $out"]

    basic = BasicDerivation(
        outputs={
            "out": DerivationOutput(
                path=out_path,
                method="",
                hash_digest="",
            ),
        },
        input_srcs=set(),
        platform=system,
        builder="/bin/sh",
        args=args,
        env={
            "builder": "/bin/sh",
            "name": drv_name,
            "out": out_path,
            "requiredSystemFeatures": feature,
            "system": system,
            "NIXBUILDNET_MIN_CPU": "1",
            "NIXBUILDNET_MAX_CPU": "1",
            "NIXBUILDNET_MIN_MEM": "128",
            "NIXBUILDNET_MAX_MEM": "128",
        },
    )
    return drv_path, basic


async def _probe_one(
    store: LocalSocketStore | SSHSubprocessStore,
    feature: str,
) -> tuple[str, bool]:
    """Probe a single feature against a store. Returns (feature, accepted)."""
    drv_path, basic = make_probe_derivation(feature)

    log.info(
        "feature_probe_start",
        store_id=store.id,
        feature=feature,
        drv_path=str(drv_path),
    )

    request = BuildDerivationRequest(
        drv_path=drv_path,
        derivation=basic,
        build_mode=BuildMode.NORMAL,
    )

    try:
        resp = await store.call(request, raise_on_error=True)
        status = resp.result.status
        error_msg = resp.result.error_msg
        if status == 0:
            log.info("feature_probe_accepted", store_id=store.id, feature=feature)
            return feature, True
        else:
            log.info(
                "feature_probe_denied",
                store_id=store.id,
                feature=feature,
                status=status,
                error_msg=error_msg,
            )
            return feature, False
    except Exception as e:
        log.info(
            "feature_probe_exception",
            store_id=store.id,
            feature=feature,
            error=str(e),
        )
        return feature, False


async def _probe_features(store: LocalSocketStore | SSHSubprocessStore) -> None:
    results = await asyncio.gather(
        *[_probe_one(store, f) for f in sorted(KNOWN_FEATURES)]
    )

    accepted = {f for f, ok in results if ok}
    denied = {f for f, ok in results if not ok}

    log.info(
        "feature_probe_summary",
        store_id=store.id,
        accepted=sorted(accepted),
        denied=sorted(denied),
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

    await _probe_features(store)


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

    await _probe_features(store)
