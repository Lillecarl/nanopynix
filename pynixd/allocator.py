from __future__ import annotations

from collections.abc import Mapping, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .build_queue import QueuedBuild
    from .store import Store

log = structlog.get_logger(__name__)

# Duration threshold for "tiny" builds that can be fast-tracked to the local store (2.5s)
TINY_BUILD_THRESHOLD_MS = 2500


@dataclass
class RankedStore:
    store_id: str
    score: int
    slots: int
    store: Store


class RankedStores:
    def __init__(self, stores: list[RankedStore]) -> None:
        self._stores = stores

    def __iter__(self) -> Iterator[RankedStore]:
        return iter(self._stores)

    def __len__(self) -> int:
        return len(self._stores)

    def __bool__(self) -> bool:
        return bool(self._stores)

    def with_slots(self) -> RankedStores:
        return RankedStores([s for s in self._stores if s.slots > 0])

    def sort(self) -> RankedStores:
        return RankedStores(
            sorted(self._stores, key=lambda s: (s.score, s.slots), reverse=True)
        )


class BuildAllocator:
    """Ranks and selects stores for a build."""

    def __init__(
        self,
        stores: Mapping[str, Store],
        local_store: Store,
        local_building: bool = False,
    ) -> None:
        self.stores = stores
        self.local_store = local_store
        self.local_building = local_building

    def rank_stores(self, build: QueuedBuild) -> RankedStores:
        """Rank stores for a build by path overlap, tiebreak by available slots."""
        build_features = build.request.derivation.effective_required_features
        stores = []

        # Check local store first (as a candidate)
        if self.local_building and self.local_store.is_healthy and not self.local_store.draining:
            if self.local_store.supports_derivation(build.platform, build_features):
                if "local" not in build.failed_backends:
                    score = self.local_store.tracker.count_common_paths(build.required_paths)
                    stores.append(RankedStore("local", score, self.local_store.available_slots, self.local_store))

        for store_id, store in self.stores.items():

            if not store.is_healthy or store.draining:
                continue
            if not store.supports_derivation(build.platform, build_features):
                continue
            if store_id in build.failed_backends:
                continue
            if store.cpu_util is not None and store.cpu_util.utilization > 99.0:
                continue

            score = store.tracker.count_common_paths(build.required_paths)
            stores.append(RankedStore(store_id, score, store.available_slots, store))

        return RankedStores(stores).sort()

    def incompatibility_reasons(
        self, platform: str, features: set[str] | None
    ) -> list[str]:
        """Build per-store incompatibility explanations for error reporting."""
        reasons: list[str] = []
        fm = self.local_store._feature_matrix
        if fm is not None and platform not in fm:
            reasons.append(f"local: system {platform} not in feature_matrix")
        elif fm is not None and features:
            local_feats = fm.get(platform, set())
            missing = features - local_feats
            if missing:
                reasons.append(
                    f"local: missing features {', '.join(sorted(missing))} for {platform}"
                )
        elif fm is None:
            reasons.append("local: no feature_matrix (not probed)")
        else:
            reasons.append("local: compatible")

        for store in self.stores.values():
            sfm = store._feature_matrix
            if sfm is not None and platform not in sfm:
                reasons.append(f"{store.id}: system {platform} not in feature_matrix")
            elif sfm is not None and features:
                store_feats = sfm.get(platform, set())
                missing = features - store_feats
                if missing:
                    reasons.append(
                        f"{store.id}: missing features {', '.join(sorted(missing))} for {platform}"
                    )
                else:
                    reasons.append(
                        f"{store.id}: compatible but excluded (unhealthy/saturated/failed)"
                    )
            elif sfm is None:
                reasons.append(f"{store.id}: no feature_matrix (not probed)")
            else:
                reasons.append(
                    f"{store.id}: compatible but excluded (unhealthy/saturated/failed)"
                )

        return reasons

    @staticmethod
    def strip_handled_features(build: QueuedBuild) -> None:
        """Remove pynixd-handled features from requiredSystemFeatures in env.

        After resolution, features like ca-derivations are no longer relevant
        to the backend daemon — pynixd already converted the derivation.
        Stripping them allows Lix stores (no ca-derivations support) to build
        resolved CA derivations that are now plain InputAddressed builds.
        """
        from .system_features import PYNIXD_HANDLED_FEATURES

        raw = build.request.derivation.env.get("requiredSystemFeatures", "")
        if not raw:
            return
        features = set(raw.split())
        stripped = features & PYNIXD_HANDLED_FEATURES
        if not stripped:
            return
        remaining = features - PYNIXD_HANDLED_FEATURES
        new_val = " ".join(sorted(remaining))
        build.request.derivation.env["requiredSystemFeatures"] = new_val
        log.debug(
            "stripped_handled_features",
            build_id=build.id,
            stripped=sorted(stripped),
            remaining=sorted(remaining),
        )
