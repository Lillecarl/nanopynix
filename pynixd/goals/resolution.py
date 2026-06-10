"""Resolution goals — computing derivation output paths via unparsing.

A ``ResolutionGoal`` resolves ONE output of ONE derivation by:

1. Reading the .drv file (lazy, cached for the goal's lifetime).
2. Creating child ``ResolutionGoal``\\s for each input dependency output.
3. After children finish, collecting resolved paths and modulo hashes.
4. Computing its own output path via ``hashDerivationModulo`` (ATerm
   serialization → SHA256 → output path derivation).
5. Trying substitution for CA-floating outputs (query realisations).
6. Returning a ``GoalResult`` with ``resolved_outputs``, ``produced_paths``,
   and ``modulo_hash``.

Resolution goals are **made to fail**: if they cannot resolve (e.g. a
CA-floating output with no known realisations), they return an empty
result and the parent ``BuildGoal`` falls back to building.
"""

from __future__ import annotations

import hashlib

import structlog

from ..drv_parser import ChildMapNode, Derivation, DrvOutput
from ..store_path import StorePath
from ..types import DerivationOutput, OutputKind
from ..types.build import BuildResult, BuildResultStatus
from ._helpers import _child_map_to_paths, _derive_output_paths, _dp_from, _fake_dp, _find_output, _unparse_for_hash
from .derivation import DerivationGoal
from .goal import EndGoal, Goal, GoalContext, GoalResult, make_build_goal, make_resolution_goal

log = structlog.get_logger(__name__)


class ResolutionGoal(Goal):
    """Resolve ONE output of ONE derivation via hashDerivationModulo.

    This is the core "unparsing" goal.  It computes the concrete store
    path for a single derivation output by:

    * Parsing the .drv ATerm.
    * Resolving all input derivation outputs (children).
    * Computing ``hashDerivationModulo``.
    * Deriving the output path from the hash.
    * Trying substitution (CA-floating) or returning the computed path.

    Keyed in the ``GoalManager`` by ``(drv_path, output_name)`` so that
    two build goals needing the same resolved output share one result.
    """

    def __init__(
        self,
        drv_path: StorePath,
        output_name: str,
        ctx: GoalContext,
    ) -> None:
        super().__init__(ctx)
        self._drv_path = drv_path
        self._output_name = output_name
        self._derivation: Derivation | None = None

    # ── identity ───────────────────────────────────────────────────

    # ── lazy derivation read (cached) ──────────────────────────────

    async def _get_derivation(self) -> Derivation | None:
        if self._derivation is None:
            self._derivation = await self.ctx.store.read_derivation(
                self._drv_path,
            )
        return self._derivation

    # ── execute ────────────────────────────────────────────────────

    async def execute(self) -> None:
        derivation = await self._get_derivation()
        if derivation is None:
            log.warning("resolve_drv_not_found", drv_path=self._drv_path)
            self.result = GoalResult(
                path=_fake_dp(self._drv_path, self._output_name),
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if self.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.MISC_FAILURE,
                ),
            )
            return

        # Find this output in the derivation
        output_obj = _find_output(derivation, self._output_name)
        if output_obj is None:
            log.warning(
                "resolve_output_not_found",
                drv_path=self._drv_path,
                output=self._output_name,
            )
            self.result = GoalResult(
                path=_fake_dp(self._drv_path, self._output_name),
                result=BuildResult(status=BuildResultStatus.MISC_FAILURE),
            )
            return

        dop = DerivationOutput(
            path=output_obj.path,
            method=output_obj.hash_algo,
            hash_digest=output_obj.hash_value,
        )

        # ── Known-path outputs — no resolution needed ─────────────
        #
        # ResolutionGoal never sets ``produced_paths`` to its own resolved
        # output — those paths are the derivation's *outputs*, not inputs.
        # If they leaked into a parent's ``input_srcs`` via
        # ``_do_build()``\'s children iteration, the daemon would reject
        # them as non-existent inputs.
        #
        # FUTURE: expressing richer goal metadata (e.g. which derivation
        # produced each path) would let the parent filter instead of
        # relying on this convention.
        if dop.kind in (OutputKind.INPUT_ADDRESSED, OutputKind.CA_FIXED):
            resolved = StorePath(output_obj.path)
            self.result = GoalResult(
                path=_fake_dp(self._drv_path, self._output_name),
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                resolved_outputs={self._output_name: resolved},
            )
            return

        # ── Deferred — must compute hashDerivationModulo ──────────
        if dop.kind == OutputKind.DEFERRED:
            await self._resolve_deferred(derivation)
            return

        # ── CA-floating — try substitution, else empty (→ parent builds) ─
        if dop.kind == OutputKind.CA_FLOATING:
            await self._resolve_floating(derivation, dop, output_obj)
            return

        # ── Impure — cannot resolve, empty result triggers build ──
        if dop.kind == OutputKind.IMPURE:
            self.result = GoalResult(
                path=_fake_dp(self._drv_path, self._output_name),
                result=BuildResult(status=BuildResultStatus.MISC_FAILURE),
            )
            return

        # ── Unknown output kind ───────────────────────────────────
        log.warning(
            "resolve_unknown_output_kind",
            drv_path=self._drv_path,
            output=self._output_name,
            kind=dop.kind,
        )
        self.result = GoalResult(
            path=_fake_dp(self._drv_path, self._output_name),
            result=BuildResult(status=BuildResultStatus.MISC_FAILURE),
        )

    # ── Deferred resolution (hashDerivationModulo + unparsing) ─────

    async def _resolve_deferred(self, derivation: Derivation) -> None:
        # 1. Create child BuildGoals for every (input_drv, output_name).
        #    We need BuildGoals rather than ResolutionGoals because input
        #    derivations may have CA-floating outputs that must be built
        #    before we can resolve our own deferred outputs.
        for input_drv_path, output_names in derivation.input_drvs.items():
            for oname in output_names:
                child = make_build_goal(
                    _dp_from(input_drv_path, oname),
                    self.ctx,
                )
                registered = self.ctx.goal_manager.register(child)
                self.add_child(registered)

        for input_drv_path, node in derivation.dynamic_input_drvs.items():
            for dp in _child_map_to_paths(input_drv_path, node):
                child = make_build_goal(dp, self.ctx)
                registered = self.ctx.goal_manager.register(child)
                self.add_child(registered)

        # 2. Execute children (builds all input derivation outputs)
        await self.execute_children()

        # 3. Collect resolved outputs, modulo hashes, and produced_paths
        #    from children.
        #
        # ``produced_paths`` here aggregates only what dependencies
        # *actually* built/substituted (e.g. CA outputs that were built).
        # Our own resolved outputs stay in ``resolved_outputs`` and must
        # NOT leak into ``produced_paths`` — they'd pollute the parent's
        # ``input_srcs`` and cause "dependency failed" errors.
        child_resolved: dict[str, StorePath] = {}
        child_modulo_hashes: dict[str, str] = {}  # drv_path → modulo_hash
        child_produced: set[StorePath] = set()
        for child in self.children:
            if child.result:
                child_resolved.update(child.result.resolved_outputs)
                child_produced.update(child.result.produced_paths)
                # ``DynamicBuildGoal`` doesn't propagate modulo_hash from
                # its outer child — traverse children to find it.
                if isinstance(child, DerivationGoal) and child.result and child.result.modulo_hash:
                    child_modulo_hashes[str(child.drv_path)] = child.result.modulo_hash
                for sub in child.children:
                    if isinstance(sub, DerivationGoal) and sub.result and sub.result.modulo_hash:
                        child_modulo_hashes[str(sub.drv_path)] = sub.result.modulo_hash
            else:
                log.debug(
                    "DEBUG_child_no_result",
                    child_type=type(child).__name__,
                    child_id=id(child),
                    is_executing=child.is_executing,
                    is_done=child.finished_executing.is_set(),
                )
            if child.result:
                log.debug(
                    "DEBUG_child_result_mod",
                    child_type=type(child).__name__,
                    child_id=id(child),
                    mod_hash=child.result.modulo_hash[:16] if child.result.modulo_hash else "",
                    resolved_keys=list(child.result.resolved_outputs),
                )

        # 4. Compute input_drv_hashes for hashDerivationModulo
        input_drv_hashes: dict[str, list[str]] = {}
        for input_drv_path, output_names in derivation.input_drvs.items():
            input_key = str(input_drv_path)
            mh = child_modulo_hashes.get(input_key)
            if mh:
                input_drv_hashes.setdefault(mh, []).extend(output_names)
            else:
                log.debug(
                    "resolve_child_no_modulo_hash",
                    drv_path=input_key,
                    child_keys=list(child_modulo_hashes),
                    child_mods={k: v[:12] for k, v in child_modulo_hashes.items()},
                    outputs=output_names,
                    input_repr=repr(input_drv_path),
                    input_key_repr=repr(input_key),
                )
                input_drv_hashes.setdefault("", []).extend(output_names)

        # 4b. Compute dynamic_input_drv_hashes for hashDerivationModulo.
        #     Each dynamic input drv's entry maps modulo_hash → ChildMapNode.
        dynamic_input_drv_hashes: dict[str, ChildMapNode] = {}
        for input_drv_path, node in derivation.dynamic_input_drvs.items():
            input_key = str(input_drv_path)
            mh = child_modulo_hashes.get(input_key)
            if mh:
                existing = dynamic_input_drv_hashes.get(mh)
                if existing:
                    # Merge: combine outputs from both nodes
                    existing.outputs.extend(node.outputs)
                    existing.children.update(node.children)
                else:
                    dynamic_input_drv_hashes[mh] = node
            else:
                log.debug(
                    "resolve_dyn_no_modulo_hash",
                    drv_path=input_key,
                )
                existing = dynamic_input_drv_hashes.setdefault("", ChildMapNode())
                existing.outputs.extend(node.outputs)
                existing.children.update(node.children)

        # 5. Serialize with hashes → hash → derive output paths
        aterm = _unparse_for_hash(
            derivation,
            input_drv_hashes,
            dynamic_input_drv_hashes=dynamic_input_drv_hashes or None,
        )
        modulo_hash = hashlib.sha256(aterm.encode()).hexdigest()

        log.debug(
            "DEBUG_resolve_deferred_hash",
            drv_path=str(self._drv_path),
            input_drv_hashes=input_drv_hashes,
            modulo_hash=modulo_hash,
            aterm_len=len(aterm),
        )

        # 6. Derive all output paths from the modulo hash
        all_resolved = _derive_output_paths(derivation, modulo_hash, self._drv_path)

        log.debug(
            "DEBUG_resolve_deferred_paths",
            drv_path=str(self._drv_path),
            all_resolved={k: str(v) for k, v in all_resolved.items()},
        )

        # 7. Build result with our specific output
        our_path = all_resolved.get(self._output_name)
        if our_path is None:
            log.warning(
                "resolve_output_not_derived",
                drv_path=self._drv_path,
                output=self._output_name,
                known_outputs=list(all_resolved),
            )
            self.result = GoalResult(
                path=_fake_dp(self._drv_path, self._output_name),
                result=BuildResult(status=BuildResultStatus.MISC_FAILURE),
            )
            return

        self.result = GoalResult(
            path=_fake_dp(self._drv_path, self._output_name),
            result=BuildResult(status=BuildResultStatus.BUILT),
            resolved_outputs={self._output_name: our_path},
            produced_paths=child_produced,
            modulo_hash=modulo_hash,
        )

    # ── CA-floating resolution (try substitution by DrvOutput) ────

    async def _resolve_floating(
        self,
        derivation: Derivation,
        dop: DerivationOutput,
        output_obj: object,
    ) -> None:
        # 1. Resolve children first
        for input_drv_path, output_names in derivation.input_drvs.items():
            for oname in output_names:
                child = make_resolution_goal(input_drv_path, oname, self.ctx)
                registered = self.ctx.goal_manager.register(child)
                self.add_child(registered)

        for input_drv_path, node in derivation.dynamic_input_drvs.items():
            for outer_out in node.direct_outputs():
                child = make_resolution_goal(input_drv_path, outer_out, self.ctx)
                registered = self.ctx.goal_manager.register(child)
                self.add_child(registered)

        await self.execute_children()

        # Strip "r:" prefix from hash algo (the .drv ATerm stores "r:sha256"
        # for recursive, but the wire protocol expects just "sha256").
        raw_algo = dop.method
        clean_algo = raw_algo.removeprefix("r:") if raw_algo else raw_algo

        # Collect produced_paths from children (dependency outputs that
        # were actually built/substituted — not our own resolved path).
        child_produced: set[StorePath] = set()
        for child in self.children:
            if child.result:
                child_produced.update(child.result.produced_paths)

        # Only try substitution when there IS a known hash (fixed-output CA).
        # Floating CA (hash_value="") cannot be substituted — we must build.
        if dop.hash_digest:
            # 2. Try substitution by DrvOutput (local)
            drv_output = DrvOutput(
                hash_algo=clean_algo,
                hash_value=dop.hash_digest,
                output_name=self._output_name,
                path="",
            )

            try:
                from ..operations.ca_derivations import QueryRealisationRequest

                resp = await self.ctx.store.execute(
                    QueryRealisationRequest(drv_output=drv_output),
                )
                if resp.realisations:
                    for r in resp.realisations:
                        if r.out_path:
                            sp = r.out_path.with_store_prefix()
                            log.info(
                                "resolve_ca_substituted",
                                drv_path=self._drv_path,
                                output=self._output_name,
                                path=sp,
                            )
                            self.result = GoalResult(
                                path=_fake_dp(self._drv_path, self._output_name),
                                result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                                resolved_outputs={self._output_name: sp},
                                produced_paths=child_produced,
                            )
                            return
            except Exception:
                log.debug(
                    "resolve_ca_query_failed",
                    drv_path=self._drv_path,
                    output=self._output_name,
                    exc_info=True,
                )

            # 3. Try remote substituters
            from pynixd.store_path import DrvOutput as DrvOutputKey

            do_key = DrvOutputKey(
                hash_algo=clean_algo,
                hash_value=dop.hash_digest,
                output_name=self._output_name,
            )
            remote = await self.ctx.substitution_manager.query_realisations({do_key})
            if remote:
                r = next(iter(remote.values()))
                if r.out_path:
                    sp = r.out_path.with_store_prefix()
                    log.info(
                        "resolve_ca_remote",
                        drv_path=self._drv_path,
                        output=self._output_name,
                        path=sp,
                    )
                    self.result = GoalResult(
                        path=_fake_dp(self._drv_path, self._output_name),
                        result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                        resolved_outputs={self._output_name: sp},
                        produced_paths=child_produced,
                    )
                    return

        # 4. Cannot resolve — empty result signals parent to build
        log.info(
            "resolve_ca_not_found",
            drv_path=self._drv_path,
            output=self._output_name,
            hash_value=bool(dop.hash_digest),
        )
        self.result = GoalResult(
            path=_fake_dp(self._drv_path, self._output_name),
            result=BuildResult(
                status=BuildResultStatus.UNKNOWN
                if self.ctx.end_goal is EndGoal.QUERY
                else BuildResultStatus.NO_SUBSTITUTERS,
            ),
        )
