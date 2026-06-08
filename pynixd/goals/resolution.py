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

from ..derived_path import DerivedPath
from ..drv_parser import Derivation, DrvOutput, _aterm_escape, read_drv_file
from ..store_path import StorePath
from ..types import DerivationOutput, OutputKind
from ..types.build import BuildResult, BuildResultStatus
from ..utils import compress_hash, nix32_encode
from .goal import EndGoal, Goal, GoalContext, GoalKey, GoalResult, make_build_goal, make_resolution_goal

log = structlog.get_logger(__name__)




# ── ResolutionGoal ─────────────────────────────────────────────────


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

    @property
    def key(self) -> GoalKey:
        return GoalKey.resolve(self._drv_path, self._output_name)

    # ── lazy derivation read (cached) ──────────────────────────────

    async def _get_derivation(self) -> Derivation | None:
        if self._derivation is None:
            self._derivation = await read_drv_file(
                self.ctx.store.store_path,
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

        for input_drv_path, output_deps in derivation.dynamic_input_drvs.items():
            for outer_out, inner_outs in output_deps.items():
                for inner_out in inner_outs:
                    # Create a nested DerivedPath (e.g.
                    # ``producingDrv!out!out``) so that ``make_build_goal``
                    # creates a ``DynamicBuildGoal`` which builds the
                    # full chain: outer → produce .drv → inner → output.
                    from ..derived_path import OutputsNames as _ON

                    child = make_build_goal(
                        DerivedPath._from_components(
                            drv_path=input_drv_path,
                            chain=(outer_out,),
                            outputs=_ON(frozenset({inner_out})),
                        ),
                        self.ctx,
                    )
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
                if child.result.modulo_hash:
                    child_modulo_hashes[str(child.key.path)] = child.result.modulo_hash
                for sub in child.children:
                    if sub.result and sub.result.modulo_hash:
                        child_modulo_hashes[str(sub.key.path)] = sub.result.modulo_hash
            else:
                log.debug(
                    "DEBUG_child_no_result",
                    key=str(child.key),
                    is_executing=child.is_executing,
                    is_done=child.finished_executing.is_set(),
                )
            if child.result:
                log.debug(
                    "DEBUG_child_result_mod",
                    key=str(child.key),
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
        #     Each dynamic input drv's entry maps outer_output → [inner_outs]
        #     using the same modulo hash as collected above.
        dynamic_input_drv_hashes: dict[str, dict[str, list[str]]] = {}
        for input_drv_path, output_deps in derivation.dynamic_input_drvs.items():
            input_key = str(input_drv_path)
            mh = child_modulo_hashes.get(input_key)
            if mh:
                dyn_entry: dict[str, list[str]] = {}
                for outer_out, inner_outs in output_deps.items():
                    dyn_entry[outer_out] = list(inner_outs)
                existing = dynamic_input_drv_hashes.get(mh)
                if existing:
                    existing.update(dyn_entry)
                else:
                    dynamic_input_drv_hashes[mh] = dyn_entry
            else:
                log.debug(
                    "resolve_dyn_no_modulo_hash",
                    drv_path=input_key,
                )
                # Use empty hash as fallback (same as the flat case)
                dyn_entry = {}
                for outer_out, inner_outs in output_deps.items():
                    dyn_entry[outer_out] = list(inner_outs)
                dynamic_input_drv_hashes.setdefault("", {}).update(dyn_entry)

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

        for input_drv_path, output_deps in derivation.dynamic_input_drvs.items():
            for outer_out, inner_outs in output_deps.items():
                for _inner_out in inner_outs:
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



def _fake_dp(drv_path: StorePath, output_name: str) -> DerivedPath:
    """Build a DerivedPath for the goal's path field."""
    from ..derived_path import OutputsNames

    return DerivedPath._from_components(
        drv_path=drv_path,
        chain=(),
        outputs=OutputsNames(frozenset({output_name})),
    )


def _find_output(
    derivation: Derivation,
    output_name: str,
) -> DrvOutput | None:
    """Find a DrvOutput by name in the derivation."""
    for o in derivation.outputs:
        if o.name == output_name:
            return o
    return None


def _dp_from(drv_path: StorePath, output_name: str) -> "DerivedPath":
    """Construct a DerivedPath for (drv_path, output_name)."""
    from ..derived_path import DerivedPath as DP, OutputsNames

    return DP._from_components(
        drv_path=drv_path,
        chain=(),
        outputs=OutputsNames(frozenset({output_name})),
    )


def _nix_drv_name(drv_path: StorePath) -> str:
    """Extract the derivation name (without .drv suffix) from a store path."""
    name = str(drv_path).rsplit("/", 1)[-1]
    first_dash = name.find("-")
    if first_dash == -1:
        return name
    name = name[first_dash + 1 :]
    return name.removesuffix(".drv")


def _output_path_name(drv_name: str, output_name: str) -> str:
    """Nix's ``outputPathName`` — the basename part of an output store path."""
    if output_name == "out":
        return drv_name
    return f"{drv_name}-{output_name}"


def _q(s: str) -> str:
    """ATerm-quote a string: wrap in quotes with escaping."""
    return f'"{_aterm_escape(s)}"'


def _unparse_for_hash(
    derivation: Derivation,
    input_drv_hashes: dict[str, list[str]],
    dynamic_input_drv_hashes: dict[str, dict[str, list[str]]] | None = None,
) -> str:
    """Serialize a Derivation to ATerm for hashDerivationModulo.

    Replaces input_drvs keys with the given modulo hashes (matching
    what Nix's hashDerivationModulo produces).  When no hashes are
    provided, input_drvs serializes as empty (collapsed).

    For derivations with ``dynamic_input_drvs``, the nested dependency
    tree is serialized using ``DrvWithVersion`` format with
    ``DerivedPathMapNode`` children.  ``dynamic_input_drv_hashes``
    maps modulo hash to ``{outer_output: [inner_outputs]}``.

    Mirrors the C++ ``Derivation::unparse()`` with actualInputs set.
    """
    parts: list[str] = []

    # Choose format
    is_dynamic = bool(derivation.dynamic_input_drvs)
    if is_dynamic:
        parts.append("DrvWithVersion(")
        parts.append(_q("xp-dyn-drv"))
        parts.append(",")
    else:
        parts.append("Derive(")

    # --- Outputs (masked for hashing) ---
    parts.append("[")
    first = True
    for o in sorted(derivation.outputs, key=lambda x: x.name):
        if first:
            first = False
        else:
            parts.append(",")
        parts.append(
            "("
            + _q(o.name) + ","
            + _q("") + ","
            + _q(o.hash_algo) + ","
            + _q(o.hash_value)
            + ")"
        )
    parts.append("],")

    # --- Input derivations (replaced with modulo hashes) ---
    # Combine flat and dynamic inputs into a single map keyed by modulo hash.
    # Each entry has (flat_output_names, {outer_out: [inner_outputs]}).
    combined: dict[str, tuple[list[str], dict[str, list[str]]]] = {}
    for h, outs in input_drv_hashes.items():
        combined[h] = (list(outs), {})
    if dynamic_input_drv_hashes:
        for h, deps in dynamic_input_drv_hashes.items():
            existing = combined.get(h)
            if existing is not None:
                existing[1].update(deps)
            else:
                combined[h] = ([], dict(deps))

    parts.append("[")
    first = True
    for h, (flat_outs, dynamic_deps) in sorted(combined.items(), key=lambda x: x[0]):
        if first:
            first = False
        else:
            parts.append(",")
        quoted_outs = ",".join(_q(o) for o in flat_outs)
        if dynamic_deps:
            # Nested: (hash,([flat_outs],[(outer_out,([inner_outs]))]))
            parts.append(f"({_q(h)},(")
            parts.append(f"[{quoted_outs}]")
            parts.append(",[")
            dyn_first = True
            for outer_out, inner_outs in sorted(dynamic_deps.items()):
                if dyn_first:
                    dyn_first = False
                else:
                    parts.append(",")
                quoted_inner = ",".join(_q(o) for o in inner_outs)
                parts.append(f"({_q(outer_out)},([{quoted_inner}]))")
            parts.append("]))")
        else:
            # Flat: (hash,[out1,out2])
            parts.append(f"({_q(h)},[{quoted_outs}])")
    parts.append("],")

    # --- Input sources ---
    srcs = ",".join(_q(str(p)) for p in sorted(str(p) for p in derivation.input_srcs))
    parts.append(f"[{srcs}],")

    # --- Platform ---
    parts.append(_q(derivation.platform) + ",")

    # --- Builder ---
    parts.append(_q(derivation.builder) + ",")

    # --- Arguments ---
    args = ",".join(_q(a) for a in derivation.args)
    parts.append(f"[{args}],")

    # --- Environment (output paths masked for hashing) ---
    output_names = {o.name for o in derivation.outputs}
    parts.append("[")
    first = True
    for k, v in sorted(derivation.env.items()):
        if first:
            first = False
        else:
            parts.append(",")
        val = "" if k in output_names else v
        parts.append(f"({_q(k)},{_q(val)})")
    parts.append("])")

    return "".join(parts)


def _derive_output_paths(
    derivation: Derivation,
    modulo_hash_hex: str,
    drv_path: StorePath,
) -> dict[str, StorePath]:
    """Derive output store paths from a modulo hash."""
    drv_name = _nix_drv_name(drv_path)
    h = bytes.fromhex(modulo_hash_hex)
    result: dict[str, StorePath] = {}

    for o in derivation.outputs:
        if o.path:
            result[o.name] = StorePath(o.path)
        else:
            name = _output_path_name(drv_name, o.name)
            out = _make_store_path(f"output:{o.name}", h, name)
            result[o.name] = StorePath(out)

    return result


def _make_store_path(
    type_str: str,
    hash_modulo: bytes,
    name: str,
    store_dir: str = "/nix/store",
) -> str:
    """Nix's ``makeStorePath`` — derive a store path from a type, hash, and name."""
    hash_hex = hash_modulo.hex()
    s = f"{type_str}:sha256:{hash_hex}:{store_dir}:{name}"
    digest = hashlib.sha256(s.encode()).digest()
    compressed = compress_hash(digest, 20)
    return f"{store_dir}/{nix32_encode(compressed)}-{name}"
