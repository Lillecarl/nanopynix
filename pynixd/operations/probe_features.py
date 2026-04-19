"""Probe a store for supported system features via BuildDerivation.

Sends derivations with ``requiredSystemFeatures`` set and checks which are
accepted by the scheduling gate. This is an internal operation — it is
never dispatched from the daemon wire protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from ..system_features import KNOWN_FEATURES
from .probe_systems import _send_probe

if TYPE_CHECKING:
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class ProbeFeaturesResult:
    system_features: set[str] = field(default_factory=set)


async def probe_features(store: Store) -> ProbeFeaturesResult:
    probe_system = store.systems[0] if store.systems else "x86_64-linux"

    to_probe = sorted(KNOWN_FEATURES | store.system_features)
    probes: list[Coroutine[None, None, tuple[str, bool]]] = []
    for feature in to_probe:
        if feature == "kvm":
            args = [
                "-c",
                "test -w /dev/kvm && echo kvm > $out"
                " || { echo 'kvm: /dev/kvm not writable' >&2; exit 1; }",
            ]
        else:
            args = ["-c", f"echo {feature} > $out"]

        name = f"probe-feature-{feature}"
        extra_env: dict[str, str] = {
            "requiredSystemFeatures": feature,
            "NIXBUILDNET_MIN_CPU": "1",
            "NIXBUILDNET_MAX_CPU": "1",
            "NIXBUILDNET_MIN_MEM": "128",
            "NIXBUILDNET_MAX_MEM": "128",
        }
        probes.append(_send_probe(store, name, probe_system, feature, args, extra_env))

    results = await asyncio.gather(*probes)
    discovered = {feature for feature, (_, ok) in zip(to_probe, results) if ok}
    store.system_features = discovered

    log.info(
        "features_probed",
        store_id=store.id,
        system_features=sorted(store.system_features),
    )
    return ProbeFeaturesResult(system_features=discovered)
