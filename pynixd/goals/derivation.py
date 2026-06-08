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

import hashlib

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
from ..types import BasicDerivation, BuildMode
from ..types.build import BuildResult, BuildResultStatus
from .goal import EndGoal, Goal, GoalContext, GoalKey, GoalResult, make_resolution_goal
from .resolution import ResolutionGoal

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
            # Propagate resolved_outputs and modulo_hash from the ResolutionGoal
            # child so that upstream ``_resolve_deferred`` and placeholder
            # rewriting can find the resolved paths even when the build goal
            # short-circuits here.
            self.result = GoalResult(
                path=dp,
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                resolved_outputs={output_name: resolved_output},
                produced_paths={resolved_output},
                modulo_hash=registered.result.modulo_hash if registered.result else "",
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
        # For deferred/floating derivations, resolve the derivation
        # before sending to the daemon: fill in output paths, rewrite
        # downstream placeholders in env/args/builder, and resolve
        # input dependency output paths.
        #
        # The daemon's ``BuildDerivation`` handler reads the .drv from
        # the store (which has empty output paths for deferred) and does
        # NOT call ``tryResolve``/``fillInOutputPaths`` — so we must
        # do this ourselves.
        drv_path = dp.base_store_path()
        resolved_output_paths = _collect_resolved_paths(self.children)

        if resolved_output_paths:
            from ..derivation_resolution import (
                resolve_derivation,
                resolve_dynamic_derivation,
            )

            has_dynamic = bool(derivation.dynamic_input_drvs)

            if has_dynamic:
                from ..derivation_resolution import DynamicPathMap

                dynamic_output_paths: DynamicPathMap = {}
                for child in self.children:
                    if isinstance(child, ResolutionGoal):
                        for sub in child.children:
                            if sub.result and sub.result.resolved_outputs:
                                for oname, outer_path in sub.result.resolved_outputs.items():
                                    outer_drv_path = StorePath(sub.key.path)
                                    dynamic_output_paths[(outer_drv_path, oname)] = outer_path
                                    for sp in sub.result.produced_paths:
                                        if sp.is_derivation():
                                            inner_drv = await read_drv_file(
                                                self.ctx.store.store_path,
                                                sp,
                                            )
                                            if inner_drv:
                                                for inner_o in inner_drv.outputs:
                                                    if inner_o.path:
                                                        inner_path = StorePath(inner_o.path)
                                                        dynamic_output_paths[(outer_drv_path, oname, inner_o.name)] = (
                                                            inner_path
                                                        )

                for child in self.children:
                    if isinstance(child, ResolutionGoal):
                        for sub in child.children:
                            if sub.result and sub.result.produced_paths:
                                outer_drv_path = StorePath(sub.key.path)
                                for sp in sub.result.produced_paths:
                                    if sp.is_derivation():
                                        dynamic_output_paths[(outer_drv_path,)] = sp
                basic = resolve_dynamic_derivation(
                    derivation,
                    drv_path,
                    dynamic_output_paths,
                )
            else:
                basic = resolve_derivation(
                    derivation,
                    drv_path,
                    resolved_output_paths,
                )
        else:
            from ..drv_parser import to_basic_derivation as parse_to_basic

            basic = await parse_to_basic(derivation, self.ctx.store.store_path)

        # Compute the path the resolved .drv WOULD have at, so that
        # ``queryPartialDerivationOutputMap`` falls back to the
        # in-memory ``drv->outputs`` (which has our resolved paths)
        # instead of reading the unresolved .drv from the store.
        # We don't write the file — ``isValidPath`` returns false for
        # the computed path because it was never registered.
        if resolved_output_paths:
            from ..derivation_resolution import (
                _unparse_basic_derivation as _unparse,
            )
            from ..utils import compress_hash, nix32_encode
            from .resolution import _nix_drv_name as _res_drv_name

            # The .drv store path is computed via Nix's ``TextInfo``
            # algorithm — NOT ``sha256(aterm)`` directly.  See
            # ``Derivation.compute_storepath()`` for the canonical
            # implementation; we inline the key steps here since
            # ``basic`` is a ``BasicDerivation`` (not ``Derivation``).
            aterm = _unparse(basic, mask_outputs=False)
            content_hash = hashlib.sha256(aterm.encode()).hexdigest()
            clean_name = _res_drv_name(drv_path)
            name = f"{clean_name}.drv"
            type_str = "text"
            hash_ref = f"sha256:{content_hash}"
            s = f"{type_str}:{hash_ref}:{self.ctx.store.store_path!s}:{name}"
            digest = hashlib.sha256(s.encode()).digest()
            compressed = compress_hash(digest, 20)
            drv_path = StorePath(f"/nix/store/{nix32_encode(compressed)}-{name}")

        # Merge the caller's input_srcs with the ones resolved by
        # ``resolve_derivation`` (which adds resolved input paths).
        all_srcs: set[StorePath] = set(input_srcs) | set(basic.input_srcs)
        response = await self.ctx.store.execute(
            BuildDerivationRequest(
                drv_path=drv_path,
                derivation=BasicDerivation(
                    outputs=basic.outputs,
                    input_srcs=all_srcs,
                    platform=basic.platform,
                    builder=basic.builder,
                    args=basic.args,
                    env=basic.env,
                    is_dynamic=basic.is_dynamic,
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

        # Compute modulo hash for CA derivations (needed by parent
        # deferred derivations for hashDerivationModulo).
        #
        # The daemon's ``hashDerivationModulo`` changes AFTER a CA build:
        # before build → ATerm hash (``mask_outputs=True``); after build →
        # ``sha256("fixed:out:" + orig_hash_algo + ":" + actual_hash + ":")``.
        # We must use the ORIGINAL hash_algo from the .drv (which has the
        # ``r:`` prefix for recursive), combined with the realised content
        # hash from the build output.
        child_hash: str = ""
        built_outputs = response.result.built_outputs
        if built_outputs:
            for drv_out, _ in built_outputs.items():
                if drv_out.output_name == output_name and drv_out.hash_value:
                    # Use the original hash_algo from the .drv (e.g.
                    # ``"r:sha256"``), not the stripped version from the
                    # realisation (``"sha256"``).
                    orig_algo = next(
                        (o.hash_algo for o in derivation.outputs if o.name == output_name),
                        "r:sha256",
                    )
                    content = f"fixed:out:{orig_algo}:{drv_out.hash_value}:"
                    child_hash = hashlib.sha256(content.encode()).hexdigest()
                    log.debug(
                        "DEBUG_modulo_post_build",
                        orig_algo=orig_algo,
                        hash_value=drv_out.hash_value,
                        child_hash=child_hash,
                    )
                    break
        elif not any(o.path for o in derivation.outputs):
            child_hash = derivation.hash_derivation_modulo(
                mask_outputs=True,
                input_drv_hashes={},
            ).get(output_name, "")

        self.result = GoalResult(
            path=dp,
            result=response.result,
            resolved_outputs=resolved,
            produced_paths=produced,
            modulo_hash=child_hash,
        )

        # If there's a cached ResolutionGoal for this (drv, output) that
        # returned empty, update its result so downstream resolution goals
        # can find the resolved paths.
        if resolved and child_hash:
            from .goal import GoalKey

            rk = GoalKey.resolve(drv_path, output_name)
            cached = self.ctx.goal_manager.goals.get(rk)
            if isinstance(cached, ResolutionGoal) and cached.result is not None:
                if not cached.result.resolved_outputs:
                    cached.result.resolved_outputs = dict(resolved)
                    cached.result.modulo_hash = child_hash
                    cached.result.produced_paths |= produced


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


def _collect_resolved_paths(children: set[Goal]) -> dict[str, StorePath]:
    """Collect resolved output paths from input deps in the goal tree.

    Traverses the goal tree looking for ``ResolutionGoal``\'s whose
    sub-children (``BuildGoal``\'s for input deps) have resolved CA
    outputs.  Returns a ``{output_name: store_path}`` dict suitable
    for ``resolve_derivation()``.

    NOTE: using output name as key is fine for single-output input deps,
    but duplicates (two inputs both with "out") will be overwritten.
    ``resolve_derivation()`` shares this limitation.
    """
    result: dict[str, StorePath] = {}

    def _collect(goal: Goal) -> None:
        if isinstance(goal, ResolutionGoal):
            for sub in goal.children:
                if sub.result and sub.result.resolved_outputs:
                    for oname, actual_path in sub.result.resolved_outputs.items():
                        if oname not in result:
                            result[oname] = actual_path
        for sub in goal.children:
            _collect(sub)

    for child in children:
        _collect(child)

    return result
