"""Probe a store for supported systems via BuildDerivation.

Sends trivial derivations for each candidate system and checks which are
accepted by the scheduling gate. This is an internal operation — it is
never dispatched from the daemon wire protocol.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from .build_derivation import BuildDerivationRequest
from .base import BasicDerivation, BuildMode, DerivationOutput
from ..store_path import StorePath
from ..utils import random_nix32_hash
from ..system_features import PROBE_SYSTEMS

if TYPE_CHECKING:
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class ProbeSystemsResult:
    systems: list[str] = field(default_factory=list)


async def probe_systems(store: Store) -> ProbeSystemsResult:
    if store.systems:
        candidates = list(store.systems)
    else:
        candidates = list(PROBE_SYSTEMS)

    results = await asyncio.gather(
        *[
            _send_probe(store, f"probe-system-{s}", s, "", ["-c", f"echo {s} > $out"])
            for s in candidates
        ]
    )

    discovered = list({system for system, (_, ok) in zip(candidates, results) if ok})
    store.systems = discovered

    log.info("systems_probed", store_id=store.id, systems=sorted(store.systems))
    return ProbeSystemsResult(systems=discovered)


async def _send_probe(
    store: Store,
    name: str,
    system: str,
    required_features: str,
    args: list[str],
    extra_env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    drv_hash = random_nix32_hash()
    out_path = f"/nix/store/{drv_hash}-{name}"
    drv_path = StorePath(f"/nix/store/{drv_hash}-{name}.drv")

    env: dict[str, str] = {
        "builder": "/bin/sh",
        "name": name,
        "out": out_path,
        "system": system,
    }
    if required_features:
        env["requiredSystemFeatures"] = required_features
    if extra_env:
        env.update(extra_env)

    basic = BasicDerivation(
        outputs={"out": DerivationOutput(path=out_path, method="", hash_digest="")},
        input_srcs=set(),
        platform=system,
        builder="/bin/sh",
        args=args,
        env=env,
    )
    request = BuildDerivationRequest(
        drv_path=drv_path,
        derivation=basic,
        build_mode=BuildMode.NORMAL,
    )
    try:
        resp = await store.call(request)
        accepted = resp.result.status == 0
        if accepted:
            log.debug("probe_accepted", store_id=store.id, probe=name)
        else:
            log.debug(
                "probe_denied",
                store_id=store.id,
                probe=name,
                status=resp.result.status,
                error_msg=resp.result.error_msg,
            )
        return name, accepted
    except Exception as e:
        log.debug("probe_exception", store_id=store.id, probe=name, error=str(e))
        return name, False
