"""BuildGoal for regular (non-opaque) derivation outputs.

A ``DerivationBuildGoal`` takes a ``DerivedPath`` like ``a.drv!out``
and ensures the output path exists.  It does this by:

1. Creating a ``ResolutionGoal`` child that resolves the output path
   (via hashDerivationModulo for deferred, or reading the .drv for
   known outputs, or querying realisations for CA-floating).
2. Checking validity of the resolved path.
3. Trying substitution if not valid.
4. Building via ``BuildDerivationRequest`` if all else fails.

For CA-floating outputs that couldn't be resolved, the build step
produces the realisations, which are then registered.
"""

from __future__ import annotations

import structlog

from ..derived_path import DerivedPath  # noqa: TC001 — used in function bodies
from ..drv_parser import (
    Derivation,
    read_drv_file,
)
from ..operations.build_derivation import BuildDerivationRequest
from ..operations.ca_derivations import RegisterDrvOutputRequest
from ..operations.is_valid_path import IsValidPathRequest
from ..store_path import StorePath
from ..types import BasicDerivation, BuildMode, DerivationOutput
from ..types.build import BuildResult, BuildResultStatus
from .goal import EndGoal, Goal, GoalContext, GoalKey, GoalResult, make_resolution_goal

log = structlog.get_logger(__name__)


class DerivationBuildGoal(Goal):
    """Ensure a single derivation output exists — resolve, substitute, or build.

    One goal per ``(drv_path, output_name)`` pair.  The ``execute``
    method:

    1. Creates a ``ResolutionGoal`` child for this output.
    2. After resolution, checks if the resolved path is valid.
    3. If valid → done (``ALREADY_VALID``).
    4. If not valid → tries substitution.
    5. If substitution fails → builds via ``BuildDerivationRequest``.
    """

    def __init__(self, derived_path: DerivedPath, ctx: GoalContext) -> None:
        super().__init__(ctx)
        self._derived_path = derived_path

    @property
    def key(self) -> GoalKey:
        return GoalKey.build(self._derived_path)

    async def execute(self) -> None:
        dp = self._derived_path
        drv_path = dp.base_store_path()
        output_name = _single_output(dp)

        log.info(
            "execute_build",
            derived_path=dp.derived,
            output=output_name,
        )

        # ── 1. Create ResolutionGoal child ────────────────────────
        resolve_goal = make_resolution_goal(drv_path, output_name, self.ctx)
        registered = self.ctx.goal_manager.register(resolve_goal)
        self.add_child(registered)

        # ── 2. Execute children (resolves input deps + this output) ─
        await self.execute_children()

        if registered.result is None or not registered.result.resolved_outputs:
            # Resolution failed (e.g. CA-floating with no known realisation).
            # We must build directly — read the .drv and send BuildDerivation.
            log.info(
                "build_resolution_failed_building",
                derived_path=dp.derived,
            )
            await self._build_fallback(dp)
            return

        resolved_output = registered.result.resolved_outputs.get(output_name)
        if resolved_output is None:
            log.warning(
                "build_resolution_missing_output",
                derived_path=dp.derived,
                output=output_name,
            )
            await self._build_fallback(dp)
            return

        # ── 3. Check validity ─────────────────────────────────────
        drv_path = dp.base_store_path()
        if (await self.ctx.store.execute(IsValidPathRequest(path=resolved_output))).valid:
            log.info("build_already_valid", path=resolved_output)
            self.result = GoalResult(
                path=dp,
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                produced_paths={resolved_output},
            )
            return

        # ── 4. Delegate to OpaqueBuildGoal for substitution ───────
        # OpaqueBuildGoal handles reference resolution and substitution
        # correctly (creates children for references, then substitutes).
        from ..derived_path import DerivedPath as DP
        from .opaque import OpaqueBuildGoal
        opaque = OpaqueBuildGoal(
            derived_path=DP._from_components(
                drv_path=resolved_output,
                chain=(),
                outputs=None,
            ),
            ctx=self.ctx,
        )
        registered = self.ctx.goal_manager.register(opaque)
        self.add_child(registered)
        await self.execute_children()
        if registered.result and registered.result.produced_paths:
            self.result = registered.result
            self.result.path = dp
            return

        # ── 5. Build ──────────────────────────────────────────────
        if self.ctx.end_goal is EndGoal.QUERY:
            self.result = GoalResult(
                path=dp,
                result=BuildResult(status=BuildResultStatus.UNKNOWN),
            )
            return

        log.info("build_executing", derived_path=dp.derived)
        await self._do_build(dp)

    # ── Build fallback (when resolution didn't produce a path) ─────

    async def _build_fallback(self, dp: DerivedPath) -> None:
        """Build a derivation whose output path wasn't resolved.

        This happens for CA-floating outputs where no prior realisation
        exists.  We read the .drv and send the build to the daemon,
        which produces the realisations.
        """
        drv_path = dp.base_store_path()
        derivation = await read_drv_file(self.ctx.store.store_path, drv_path)
        if derivation is None:
            self.result = GoalResult(
                path=dp,
                result=BuildResult(
                    status=BuildResultStatus.MISC_FAILURE,
                    error_msg=f"Derivation not found: {drv_path}",
                ),
            )
            return

        # Collect input sources from children
        input_srcs: set[StorePath] = set(derivation.input_srcs)
        for child in self.children:
            if child.result:
                input_srcs.update(child.result.produced_paths)

        await self._do_build_with_derivation(dp, derivation, input_srcs)

    # ── Core build logic ───────────────────────────────────────────

    async def _do_build(self, dp: DerivedPath) -> None:
        """Build this derivation with pre-resolved outputs."""
        drv_path = dp.base_store_path()
        derivation = await read_drv_file(self.ctx.store.store_path, drv_path)
        if derivation is None:
            self.result = GoalResult(
                path=dp,
                result=BuildResult(
                    status=BuildResultStatus.MISC_FAILURE,
                    error_msg=f"Derivation not found: {drv_path}",
                ),
            )
            return

        # Collect all resolved paths from children
        input_srcs: set[StorePath] = set(derivation.input_srcs)
        for child in self.children:
            if child.result:
                input_srcs.update(child.result.produced_paths)

        await self._do_build_with_derivation(dp, derivation, input_srcs)

    async def _do_build_with_derivation(
        self,
        dp: DerivedPath,
        derivation: Derivation,
        input_srcs: set[StorePath],
    ) -> None:
        """Send the build request to the daemon."""
        from ..drv_parser import to_basic_derivation as parse_to_basic

        # Build the derivation env with resolved paths
        resolved_env = dict(derivation.env)
        for child in self.children:
            if child.result and child.result.resolved_outputs:
                for oname, sp in child.result.resolved_outputs.items():
                    resolved_env[oname] = str(sp)

        basic = await parse_to_basic(derivation, self.ctx.store.store_path)
        # Override env with resolved paths
        basic.env = resolved_env

        drv_path = dp.base_store_path()
        response = await self.ctx.store.execute(
            BuildDerivationRequest(
                drv_path=drv_path,
                derivation=BasicDerivation(
                    outputs={
                        o.name: DerivationOutput(
                            path=o.path,
                            method=o.hash_algo,
                            hash_digest=o.hash_value,
                        )
                        for o in derivation.outputs
                    },
                    input_srcs=input_srcs,
                    platform=derivation.platform,
                    builder=derivation.builder,
                    args=derivation.args,
                    env=resolved_env,
                    is_dynamic=derivation.is_dynamic,
                ),
                build_mode=BuildMode.NORMAL,
            )
        )

        # Register any CA realisations from the build
        for realisation in response.result.built_outputs.values():
            try:
                await self.ctx.store.execute(
                    RegisterDrvOutputRequest(realisation=realisation),
                )
            except Exception:
                log.warning(
                    "register_drv_output_failed",
                    drv_output=realisation.id,
                    exc_info=True,
                )

        # Collect produced paths
        produced: set[StorePath] = set()
        for realisation in response.result.built_outputs.values():
            if realisation.out_path:
                produced.add(realisation.out_path.with_store_prefix())
        for o in derivation.outputs:
            if o.path:
                produced.add(StorePath(o.path))

        output_name = _single_output(dp)
        resolved = {}
        # Try to get our specific output from realisations
        for drv_out, realisation in response.result.built_outputs.items():
            if drv_out.output_name == output_name and realisation.out_path:
                resolved[output_name] = realisation.out_path.with_store_prefix()
        # Fallback: derivation output paths
        if not resolved:
            for o in derivation.outputs:
                if o.name == output_name and o.path:
                    resolved[output_name] = StorePath(o.path)

        self.result = GoalResult(
            path=dp,
            result=response.result,
            resolved_outputs=resolved,
            produced_paths=produced,
        )


def _single_output(dp: DerivedPath) -> str:
    """Extract the single output name from a DerivedPath, defaulting to 'out'."""
    from ..derived_path import OutputsAll, OutputsNames

    if dp.is_opaque:
        return ""
    if isinstance(dp.outputs, OutputsAll):
        return "out"
    if isinstance(dp.outputs, OutputsNames):
        names = list(dp.outputs.names)
        return names[0] if names else "out"
    return "out"



