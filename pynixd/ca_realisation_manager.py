from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from .operations.ca_derivations import RegisterDrvOutputRequest

if TYPE_CHECKING:
    from .build_queue import QueuedBuild
    from .operations.build_derivation import BuildDerivationResponse
    from .scheduler import Scheduler
    from .store import Store

log = structlog.get_logger(__name__)


class CaRealisationManager:
    """Manages registration of content-addressed (CA) realisations across stores."""

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self.local_store = scheduler.local_store
        self.queue = scheduler.queue

    async def register_dep_realisations(self, build: QueuedBuild, store: Store) -> None:
        """Register CA realisations from completed dependency builds on the
        target builder store so it can resolve deferred output paths.
        """
        for dep_id in build.depends_on:
            dep_build = self.queue.by_id.get(dep_id)
            if dep_build is None or not dep_build.ca_realisations:
                continue

            if store is self.local_store:
                # Realisations already registered on local store during
                # the dependency build's completion
                continue

            for realisation in dep_build.ca_realisations:
                try:
                    reg_req = RegisterDrvOutputRequest(realisation=realisation)
                    log.debug(
                        "registering_dep_realisation_on_builder",
                        build_id=build.id,
                        dep_build_id=dep_id,
                        store_id=store.store_id,
                        realisation=realisation,
                    )
                    await store.call(reg_req, suppress_last=True)
                except Exception as exc:
                    log.warning(
                        "register_dep_realisation_failed",
                        build_id=build.id,
                        dep_build_id=dep_id,
                        store_id=store.store_id,
                        error=str(exc),
                    )

    async def register_built_outputs(
        self,
        build: QueuedBuild,
        resp: BuildDerivationResponse,
    ) -> None:
        """Register CA realisations from a completed build on the local store."""
        if not resp.result.built_outputs:
            return

        for drv_output_str, realisation in resp.result.built_outputs.items():
            try:
                reg_req = RegisterDrvOutputRequest(realisation=realisation)
                await self.local_store.execute(reg_req, suppress_last=True)
            except Exception:
                log.warning(
                    "register_drv_output_failed",
                    drv_output=drv_output_str,
                    exc_info=True,
                )
