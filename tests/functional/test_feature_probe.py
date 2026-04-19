"""Feature probe: send BuildDerivationRequest with requiredSystemFeatures
directly to a LocalSocketStore, constructing derivations entirely in-memory.

No .drv files on disk needed — the daemon uses the wire-provided BasicDerivation.
"""

from __future__ import annotations

import platform

import pytest
import structlog

from pynixd.operations.base import BasicDerivation, BuildMode, DerivationOutput
from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.store import LocalSocketStore
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

DUMMY_HASH = "00000000000000000000000000000000"


def make_probe_derivation(feature: str) -> tuple[StorePath, BasicDerivation]:
    """Construct a minimal BasicDerivation that requires a system feature.

    The drv_path is fake but valid-format — the daemon doesn't read .drv from
    disk for BuildDerivation, it uses the wire-provided BasicDerivation.
    """
    system = f"{platform.machine()}-{platform.system().lower()}"
    drv_name = f"probe-{feature}"
    drv_path = StorePath(f"/nix/store/{DUMMY_HASH}-{drv_name}.drv")
    out_path = f"/nix/store/{DUMMY_HASH}-{drv_name}"

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
        },
    )
    return drv_path, basic


@pytest.mark.timeout(60)
async def test_feature_probe_in_memory() -> None:
    store_path = STORE_PREFIX / "feature-probe"
    rmtree_robust(store_path)
    kwargs = get_test_store_kwargs(nix_config=FEATURE_NIX_CONFIG)
    store = LocalSocketStore(id="feature-probe", store_path=store_path, **kwargs)
    await store.ensure_daemon()

    accepted: set[str] = set()
    denied: set[str] = set()

    for feature in sorted(KNOWN_FEATURES):
        drv_path, basic = make_probe_derivation(feature)

        log.info(
            "feature_probe",
            feature=feature,
            drv_path=str(drv_path),
            required_system_features=set(
                basic.env.get("requiredSystemFeatures", "").split()
            ),
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
                accepted.add(feature)
                log.info("feature_probe_accepted", feature=feature)
            else:
                denied.add(feature)
                log.info(
                    "feature_probe_denied",
                    feature=feature,
                    status=status,
                    error_msg=error_msg,
                )
        except Exception as e:
            denied.add(feature)
            log.info("feature_probe_exception", feature=feature, error=str(e))

    log.info(
        "feature_probe_summary",
        accepted=sorted(accepted),
        denied=sorted(denied),
    )

    assert "nixos-test" in accepted
    assert "benchmark" in accepted
    assert "big-parallel" in accepted
    assert "ca-derivations" in accepted
    assert "recursive-nix" in accepted
    assert "apple-virt" in denied
    assert "uid-range" in denied
    assert "kvm" in denied
