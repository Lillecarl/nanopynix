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
from .goal import EndGoal, Goal, GoalContext, GoalKey, GoalResult, make_resolution_goal

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
        if dop.kind in (OutputKind.INPUT_ADDRESSED, OutputKind.CA_FIXED):
            resolved = StorePath(output_obj.path)
            self.result = GoalResult(
                path=_fake_dp(self._drv_path, self._output_name),
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                resolved_outputs={self._output_name: resolved},
                produced_paths={resolved},
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
        # 1. Create child ResolutionGoals for every (input_drv, output_name)
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

        # 2. Execute children (resolves input derivation outputs)
        await self.execute_children()

        # 3. Collect resolved outputs and modulo hashes from children
        child_resolved: dict[str, StorePath] = {}
        child_modulo_hashes: dict[str, str] = {}  # drv_path → modulo_hash
        for child in self.children:
            if child.result:
                if child.result.resolved_outputs:
                    child_resolved.update(child.result.resolved_outputs)
                if child.result.modulo_hash and isinstance(child, ResolutionGoal):
                        child_modulo_hashes[str(child._drv_path)] = child.result.modulo_hash

        # 4. Compute input_drv_hashes for hashDerivationModulo
        input_drv_hashes: dict[str, list[str]] = {}
        for input_drv_path, output_names in derivation.input_drvs.items():
            mh = child_modulo_hashes.get(str(input_drv_path))
            if mh:
                input_drv_hashes.setdefault(mh, []).extend(output_names)
            else:
                log.debug(
                    "resolve_child_no_modulo_hash",
                    drv_path=input_drv_path,
                    outputs=output_names,
                )
                input_drv_hashes.setdefault("", []).extend(output_names)

        # 5. Serialize with hashes → hash → derive output paths
        aterm = _unparse_for_hash(derivation, input_drv_hashes)
        modulo_hash = hashlib.sha256(aterm.encode()).hexdigest()

        # 6. Derive all output paths from the modulo hash
        all_resolved = _derive_output_paths(derivation, modulo_hash, self._drv_path)

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

        produced: set[StorePath] = set(all_resolved.values())
        self.result = GoalResult(
            path=_fake_dp(self._drv_path, self._output_name),
            result=BuildResult(status=BuildResultStatus.BUILT),
            resolved_outputs={self._output_name: our_path},
            produced_paths=produced,
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

        # 2. Try substitution by DrvOutput (local)
        drv_output = DrvOutput(
            hash_algo=dop.method,
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
                produced: set[StorePath] = set()
                for r in resp.realisations:
                    if r.out_path:
                        sp = r.out_path.with_store_prefix()
                        produced.add(sp)

                if produced:
                    log.info(
                        "resolve_ca_substituted",
                        drv_path=self._drv_path,
                        output=self._output_name,
                        path=produced,
                    )
                    self.result = GoalResult(
                        path=_fake_dp(self._drv_path, self._output_name),
                        result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                        resolved_outputs={self._output_name: next(iter(produced))},
                        produced_paths=produced,
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
            hash_algo=dop.method,
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
                    produced_paths={sp},
                )
                return

        # 4. Cannot resolve — empty result signals parent to build
        log.info(
            "resolve_ca_not_found",
            drv_path=self._drv_path,
            output=self._output_name,
        )
        self.result = GoalResult(
            path=_fake_dp(self._drv_path, self._output_name),
            result=BuildResult(
                status=BuildResultStatus.UNKNOWN
                if self.ctx.end_goal is EndGoal.QUERY
                else BuildResultStatus.NO_SUBSTITUTERS,
            ),
        )


# ── Helpers ─────────────────────────────────────────────────────────



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
) -> str:
    """Serialize a Derivation to ATerm for hashDerivationModulo.

    Replaces input_drvs keys with the given modulo hashes (matching
    what Nix's hashDerivationModulo produces).  When no hashes are
    provided, input_drvs serializes as empty (collapsed).

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
    parts.append("[")
    first = True
    for h, outs in sorted(input_drv_hashes.items(), key=lambda x: x[0]):
        if first:
            first = False
        else:
            parts.append(",")
        quoted_outs = ",".join(_q(o) for o in outs)
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
