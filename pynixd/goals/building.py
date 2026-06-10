"""DerivationBuildingGoal — the actual build execution.

Created by DerivationGoal after substitution attempts fail.
One goal per (drv_path, output_name), deduped by StorePath only.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog

from ..operations.build_derivation import BuildDerivationRequest
from ..store_path import StorePath
from ..types import BasicDerivation, BuildMode
from ..types.build import BuildResult, BuildResultStatus
from ._helpers import _collect_resolved_paths, _fake_dp
from .goal import Goal, GoalResult

if TYPE_CHECKING:
    from ..drv_parser import Derivation
    from .goal import GoalContext

log = structlog.get_logger(__name__)


class DerivationBuildingGoal(Goal):
    """Build a single derivation — the actual build execution.

    Created by DerivationGoal after substitution attempts fail.
    One goal per (drv_path, output_name), but deduped by StorePath only.
    """

    def __init__(
        self,
        drv_path: StorePath,
        ctx: GoalContext,
    ) -> None:
        super().__init__(ctx)
        self.drv_path = drv_path
        self.output_name: str = ""  # set by DerivationGoal before run()
        self.derivation: Derivation | None = None  # set by DerivationGoal
        self.resolved_paths: dict[str, StorePath] = {}  # set by DerivationGoal
        self.input_srcs: set[StorePath] = set()  # set by DerivationGoal

    async def execute(self) -> None:
        if self.derivation is None:
            derivation = await self.ctx.store.read_derivation(self.drv_path)
            if derivation is None:
                self.result = GoalResult(
                    path=_fake_dp(self.drv_path, self.output_name),
                    result=BuildResult(
                        status=BuildResultStatus.MISC_FAILURE,
                        error_msg=f"Derivation not found: {self.drv_path}",
                    ),
                )
                return
            self.derivation = derivation

        await self._do_build()

    async def _do_build(self) -> None:
        derivation = self.derivation
        if derivation is None:
            return

        from ..derivation_resolution import (
            resolve_derivation,
            resolve_dynamic_derivation,
        )

        drv_path = self.drv_path
        resolved_output_paths = self.resolved_paths or _collect_resolved_paths(set())

        if resolved_output_paths:
            if derivation.dynamic_input_drvs:
                from ..derivation_resolution import DynamicPathMap

                dynamic_output_paths: DynamicPathMap = {}
                # (simplified — children aren't available here, caller provides resolved_paths)
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

        # Compute the path the resolved .drv WOULD have at
        if resolved_output_paths:
            from ..derivation_resolution import (
                _unparse_basic_derivation as _unparse,
            )
            from ..utils import compress_hash, nix32_encode
            from ._helpers import _nix_drv_name as _res_drv_name

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

        # Merge input_srcs
        all_srcs: set[StorePath] = set(self.input_srcs) | set(basic.input_srcs)
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
                from ..operations.ca_derivations import RegisterDrvOutputRequest

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

        output_name = self.output_name or "out"
        resolved = {}
        for drv_out, realisation in response.result.built_outputs.items():
            if drv_out.output_name == output_name and realisation.out_path:
                resolved[output_name] = realisation.out_path.with_store_prefix()
        if not resolved:
            for o in derivation.outputs:
                if o.name == output_name and o.path:
                    resolved[output_name] = StorePath(o.path)

        # Compute modulo hash for CA derivations
        child_hash: str = ""
        built_outputs = response.result.built_outputs
        if built_outputs:
            for drv_out in built_outputs:
                if drv_out.output_name == output_name and drv_out.hash_value:
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
            path=_fake_dp(self.drv_path, output_name),
            result=response.result,
            resolved_outputs=resolved,
            produced_paths=produced,
            modulo_hash=child_hash,
        )

        # Update cached ResolutionGoal result if needed
