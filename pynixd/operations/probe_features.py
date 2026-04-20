"""Probe a store for supported system features via BuildDerivation.

An internal operation — never dispatched from the daemon wire protocol.
Constructs derivations with ``requiredSystemFeatures`` set and checks which
are accepted by the scheduling gate.  Features are probed for every
discovered system so that platform-specific features (e.g. ``apple-virt``
on Darwin, ``kvm`` on Linux) are correctly detected.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..operations.base import OpRequest, OpResponse
from ..system_features import KNOWN_FEATURES
from ..wire import NixReader, NixWriter
from .probe_systems import _send_probe

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class ProbeFeaturesResponse(OpResponse):
    system_features: set[str] = field(default_factory=set)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        pass


@dataclass
class ProbeFeaturesRequest(OpRequest[ProbeFeaturesResponse]):
    name: ClassVar[str] = "ProbeFeatures"
    op: ClassVar[int] = 109
    response_type: ClassVar[type[OpResponse]] = ProbeFeaturesResponse

    systems: set[str] = field(default_factory=lambda: {"x86_64-linux"})
    system_features: set[str] = field(default_factory=set)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        pass

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> ProbeFeaturesResponse:
        to_probe = (self.system_features or set()) | KNOWN_FEATURES
        probes = []
        for system in self.systems:
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
                probes.append(
                    _send_probe(store, name, system, feature, args, extra_env)
                )

        results = await asyncio.gather(*probes)
        system_features = {feature for feature, (_, ok) in zip(to_probe, results) if ok}
        store.system_features = system_features

        log.info(
            "features_probed",
            store_id=store.id,
            system_features=sorted(store.system_features),
        )
        return ProbeFeaturesResponse(system_features=system_features)
