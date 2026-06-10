"""DrvOutputSubstitutionGoal — query realisations for CA-floating outputs.

Tries to find a realisation for a CA-floating derivation output.
First checks the local store (QueryRealisationRequest), then queries
remote substituters via substitution_manager.query_realisations().

After success, ``output_info`` contains the discovered store path.
The caller (DerivationGoal) creates a PathSubstitutionGoal for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ..store_path import StorePath
from ..types.build import BuildResult, BuildResultStatus
from .goal import Goal, GoalContext, GoalResult

if TYPE_CHECKING:
    from ..drv_parser import DrvOutput

log = structlog.get_logger(__name__)


class DrvOutputSubstitutionGoal(Goal):
    """Query realisations: map DrvOutput → StorePath.

    Tries to find a realisation for a CA-floating derivation output.
    First checks local store (QueryRealisationRequest), then queries
    remote substituters via substitution_manager.query_realisations().

    After success, ``output_info`` contains the discovered store path.
    The CALLER (DerivationGoal) creates a PathSubstitutionGoal for it.
    """

    def __init__(
        self,
        drv_output: DrvOutput,
        ctx: GoalContext,
    ) -> None:
        super().__init__(ctx)
        self.drv_output = drv_output
        self.output_info: StorePath | None = None  # discovered outpath

    async def execute(self) -> None:
        # 1. Check local store
        from ..operations.ca_derivations import QueryRealisationRequest

        try:
            resp = await self.ctx.store.execute(
                QueryRealisationRequest(drv_output=self.drv_output),
            )
            if resp.realisations:
                r = resp.realisations[0]
                if r.out_path:
                    self.output_info = r.out_path.with_store_prefix()
                    log.info(
                        "drv_output_sub_found_local",
                        drv_output=str(self.drv_output),
                        path=self.output_info,
                    )
                    self.result = GoalResult(
                        path=self._fake_dp_empty(),
                        result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                        produced_paths={self.output_info},
                    )
                    return
        except Exception:
            log.exception("drv_output_sub_local_failed")

        # 2. Check remote substituters
        try:
            remote = await self.ctx.substitution_manager.query_realisations({self.drv_output})
        except Exception:
            log.debug("drv_output_sub_remote_failed", exc_info=True)
            remote = {}
        if remote:
            r = next(iter(remote.values()))
            if r.out_path:
                self.output_info = r.out_path.with_store_prefix()
                log.info(
                    "drv_output_sub_found_remote",
                    drv_output=str(self.drv_output),
                    path=self.output_info,
                )
                self.result = GoalResult(
                    path=self._fake_dp_empty(),
                    result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                    produced_paths={self.output_info},
                )
                return

        # 3. Not found
        log.info("drv_output_sub_not_found", drv_output=str(self.drv_output))
        self.result = GoalResult(
            path=self._fake_dp_empty(),
            result=BuildResult(status=BuildResultStatus.NO_SUBSTITUTERS),
        )

    def _fake_dp_empty(self):
        """Temporary — result path isn't meaningful for this goal."""
        from ..derived_path import DerivedPath

        return DerivedPath._from_components(
            drv_path=StorePath("/nix/store/00000000000000000000000000000000-placeholder"),
            chain=(),
            outputs=None,
        )
