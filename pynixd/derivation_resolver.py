"""Pre-build derivation resolution for deferred and dynamic derivations.

When a derivation has outputs whose paths cannot be computed before
building (CA derivations), the wire protocol sends a BasicDerivation
with placeholders and the daemon cannot resolve it. This module
resolves placeholders to actual paths before sending the build.

Two resolution modes (unified in one method):
- **Deferred**: inputDrv outputs (level-1 placeholders)
- **Dynamic**: dynamic_input_drv outputs (level-2+ placeholders, DrvWithVersion)

Also handles registering CA realisations on builder stores so they
can resolve their own deferred outputs.

The resolution decision mirrors Nix's ``Derivation::shouldResolve()``:
resolution is needed when any output is Deferred, CAFloating, or Impure,
or when any input comes from a dynamic derivation (childMap in
inputDrvs).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import asyncssh
import structlog

from .derivation_resolution import (
    _nix_drv_name,
    _unparse_basic_derivation,
)
from .derivation_resolution import (
    resolve_derivation as drv_resolve_derivation,
)
from .derivation_resolution import (
    resolve_dynamic_derivation as drv_resolve_dynamic_derivation,
)
from .drv_parser import read_drv_file
from .exceptions import BackendError
from .operations.add_to_store import AddToStoreRequest
from .operations.base import UnkeyedValidPathInfo
from .operations.ca_derivations import RegisterDrvOutputRequest
from .store_path import StorePath
from .types.derivation import OutputKind

if TYPE_CHECKING:
    from .build_queue import QueuedBuild
    from .drv_parser import Derivation
    from .operations.base import BasicDerivation
    from .operations.build_derivation import BuildDerivationResponse
    from .scheduler import Scheduler
    from .store import Store
    from .types.aliases import StorePathSet

log = structlog.get_logger(__name__)


class DerivationResolver:
    """Handles pre-build resolution of deferred and dynamic derivations.

    Called from Scheduler.execute_build() before sending a BuildDerivation
    to the backend daemon. Mutates build.request and build.required_paths
    in-place with resolved paths.

    Also registers CA realisations from dependency builds on the target
    builder store so it can resolve its own deferred outputs.
    """

    def __init__(
        self,
        scheduler: Scheduler,
    ) -> None:
        self.scheduler = scheduler
        self.local_store = scheduler.local_store
        self.queue = scheduler.queue

    def collect_missing_dep_out_paths(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> StorePathSet:
        """Collect output paths from dependency CA realisations that are
        missing on the target builder store.

        These paths must exist in the store's ValidPaths before
        RegisterDrvOutputRequest can succeed, because the Nix daemon's
        INSERT uses a subquery ``(select id from ValidPaths where path = ?)``
        for the Realisations.outputPath foreign key.
        """
        missing: StorePathSet = set()
        if store is self.local_store:
            return missing
        for dep_id in build.depends_on:
            dep_build = self.queue.by_id.get(dep_id)
            if dep_build is None or not dep_build.ca_realisations:
                continue
            for realisation in dep_build.ca_realisations:
                out_path_raw = realisation.out_path
                if out_path_raw:
                    out_path = StorePath(str(out_path_raw)).with_store_prefix()
                    if out_path not in store.tracker.known_paths:
                        missing.add(out_path)
        return missing

    async def register_dep_realisations(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> None:
        """Register CA realisations from completed dependency builds on the
        target builder store so it can resolve deferred output paths.
        """
        for dep_id in build.depends_on:
            dep_build = self.queue.by_id.get(dep_id)
            if dep_build is None or not dep_build.ca_realisations:
                continue

            if store is self.local_store:
                continue

            for realisation in dep_build.ca_realisations:
                try:
                    reg_req = RegisterDrvOutputRequest(realisation=realisation)
                    log.debug(
                        "registering_dep_realisation_on_builder",
                        build_id=build.build_id,
                        dep_build_id=dep_id,
                        store_id=store.store_id,
                        realisation=realisation,
                    )
                    await store.call(reg_req, suppress_last=True)
                except (BackendError, OSError, ConnectionError, EOFError, asyncssh.misc.Error) as exc:
                    log.warning(
                        "register_dep_realisation_failed",
                        build_id=build.build_id,
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
            except (BackendError, OSError, ConnectionError):
                log.warning(
                    "register_drv_output_failed",
                    drv_output=drv_output_str,
                    exc_info=True,
                )

    async def resolve(self, build: QueuedBuild, store: Store) -> None:
        """Resolve placeholders in a derivation before building.

        Mirrors ``Nix::Derivation::shouldResolve()`` + ``tryResolve()``:

        1. Read the .drv file from disk to inspect its outputs and
           inputDrvs (these are not in the wire BasicDerivation).
        2. If any output is Deferred, CAFloating, or Impure, or if
           any inputDrv has a childMap (dynamic derivation input),
           the derivation needs resolution.
        3. Collect resolved paths from dependency builds' CA realisations.
        4. Rewrite placeholders via ``resolve_derivation`` or
           ``resolve_dynamic_derivation`` and upload the resolved .drv.
        5. Mutate ``build.request`` and ``build.required_paths``.
        """
        drv_path = build.request.drv_path

        parsed = await self._read_drv(drv_path, build.build_id)
        if parsed is None:
            return

        if not self._should_resolve(parsed):
            return

        dep_realisations = self._collect_dep_realisations(build)

        if parsed.dynamic_input_drvs:
            dynamic_output_paths = await self._build_dynamic_output_paths(
                build,
                dep_realisations,
                parsed.dynamic_input_drvs,
            )
            if not dynamic_output_paths:
                return
            resolved = drv_resolve_dynamic_derivation(
                parsed,
                drv_path,
                dynamic_output_paths,
            )
            output_paths_for_build = set(dynamic_output_paths.values())
        else:
            flat_paths = self._flatten_realisations(parsed.input_drvs, dep_realisations)
            if not flat_paths:
                return
            resolved = drv_resolve_derivation(parsed, drv_path, flat_paths)
            output_paths_for_build = set(flat_paths.values())

        await self._add_resolved_drv(build, store, resolved, drv_path)
        self._populate_required_paths(build, resolved, output_paths_for_build)

        log.info(
            "resolved_derivation",
            build_id=build.build_id,
            drv_path=drv_path,
            resolved_drv_path=build.request.drv_path,
            output_paths={n: o.path for n, o in resolved.outputs.items()},
            mode="dynamic" if parsed.dynamic_input_drvs else "deferred",
        )

    # ── Resolution decision (mirrors Nix) ────────────────────────────

    def _should_resolve(
        self,
        parsed: Derivation,
    ) -> bool:
        """Determine whether a derivation needs resolution before building.

        Mirrors Nix's ``Derivation::shouldResolve()`` at
        ``src/libstore/derivations.cc:1125-1156``.

        Returns True if any of these hold:
        - Any output is Deferred (``("", "", "")`` — depends on a floating CA)
        - Any output is CAFloating (``("", "r:sha256", "")``)
        - Any output is Impure (``("", "...", "impure")``)
        - Any inputDrv has a childMap (dynamic derivation — DrvWithVersion)
        - The derivation has inputDrvs AND at least one of the above
          output conditions (the inputDrvs contain placeholders to rewrite)
        """
        has_resolve_trigger = False
        has_dynamic_inputs = bool(parsed.dynamic_input_drvs)

        for kind in parsed.output_kinds():
            if kind in (OutputKind.DEFERRED, OutputKind.CA_FLOATING, OutputKind.IMPURE):
                has_resolve_trigger = True
                break

        # Nix: dynamic inputs (childMap) always trigger resolution.
        if has_dynamic_inputs:
            return True

        # Nix: Deferred, CAFloating, or Impure outputs only trigger
        # resolution when there are inputDrvs to rewrite.  A pure
        # floating CA derivation with no inputs doesn't need resolution
        # (the build produces outputs naturally).
        return has_resolve_trigger and bool(parsed.input_drvs)

    # ── Internal helpers ─────────────────────────────────────────────

    async def _read_drv(
        self,
        drv_path: StorePath,
        build_id: int,
    ) -> Derivation | None:
        """Read and parse a .drv file from the local store."""
        try:
            return await read_drv_file(self.local_store.store_path, drv_path)
        except FileNotFoundError:
            log.warning(
                "resolve_drv_not_found",
                build_id=build_id,
                drv_path=drv_path,
            )
            return None

    def _collect_dep_realisations(
        self,
        build: QueuedBuild,
    ) -> dict[StorePath, dict[str, StorePath]]:
        """Collect CA realisations from completed dependency builds.

        Returns {dep_drv_path: {output_name: resolved_store_path}}.
        """
        dep_realisations: dict[StorePath, dict[str, StorePath]] = {}
        for dep_id in build.depends_on:
            dep_build = self.queue.by_id.get(dep_id)
            if dep_build is None or not dep_build.ca_realisations:
                continue
            dep_drv_path = StorePath(dep_build.request.drv_path)
            for realisation in dep_build.ca_realisations:
                out_path = realisation.out_path
                output_name = str(realisation.id).rsplit("!", 1)[-1] or "out"
                if out_path:
                    dep_realisations.setdefault(dep_drv_path, {})[output_name] = StorePath(out_path).with_store_prefix()
        return dep_realisations

    def _flatten_realisations(
        self,
        input_drvs: dict[StorePath, list[str]],
        dep_realisations: dict[StorePath, dict[str, StorePath]],
    ) -> dict[str, StorePath]:
        """Flatten dep_realisations into {output_name: path} for deferred resolution.

        Matches input_drvs entries to available realisations. This is the
        simple (level-1) case — no nested .drv outputs.
        """
        flat: dict[str, StorePath] = {}
        for input_drv_path, output_names in input_drvs.items():
            inner = dep_realisations.get(input_drv_path, {})
            for oname in output_names:
                sp = inner.get(oname)
                if sp is not None:
                    flat[oname] = sp
        return flat

    async def _build_dynamic_output_paths(
        self,
        build: QueuedBuild,
        dep_realisations: dict[StorePath, dict[str, StorePath]],
        dynamic_input_drvs: dict[StorePath, dict[str, list[str]]],
    ) -> dict[tuple[StorePath, str, str], StorePath]:
        """Build the dynamic_output_paths map for DrvWithVersion resolution.

        Resolves two levels for each dynamic_input_drvs entry:

        1. **Level 1** — outer drv output (= .drv path): looked up from
           ``dep_realisations`` (populated from completed dep builds' CA
           realisations).

        2. **Level 2+** — inner .drv output (= actual output): looked up
           from ``dep_realisations`` (if the inner .drv was also a dep build
           that completed).  Falls back to reading the .drv file from disk
           to get static output paths if the realisations aren't available.

        The key type is ``(dyn_drv_path, outer_output, inner_output_name)``
        which matches the DrvWithVersion wire format.

        Returns an empty dict if no paths can be resolved (warnings logged).
        """
        dynamic_output_paths: dict[tuple[StorePath, str, str], StorePath] = {}

        for dyn_drv_path, output_deps in dynamic_input_drvs.items():
            outer_outputs = dep_realisations.get(dyn_drv_path, {})

            for outer_output, inner_outputs in output_deps.items():
                level1_path = outer_outputs.get(outer_output)
                if level1_path is None:
                    log.warning(
                        "resolve_dynamic_no_outer_output",
                        build_id=build.build_id,
                        drv_path=dyn_drv_path,
                        output=outer_output,
                    )
                    continue

                for inner_output_name in inner_outputs:
                    if level1_path.is_derivation():
                        inner_outputs_map = dep_realisations.get(level1_path, {})
                        actual_path = inner_outputs_map.get(inner_output_name)

                        if not actual_path:
                            try:
                                inner_parsed = await read_drv_file(
                                    self.local_store.store_path,
                                    level1_path,
                                )
                                if inner_parsed is None:
                                    continue
                                inner_outs = inner_parsed.output_paths()
                                actual_path = inner_outs.get(inner_output_name)
                            except (OSError, ValueError) as e:
                                log.warning(
                                    "resolve_dynamic_read_drv_failed",
                                    drv_path=str(level1_path),
                                    error=str(e),
                                )

                        if actual_path:
                            dynamic_output_paths[(dyn_drv_path, outer_output, inner_output_name)] = actual_path
                    else:
                        dynamic_output_paths[(dyn_drv_path, outer_output, inner_output_name)] = level1_path

        return dynamic_output_paths

    async def _add_resolved_drv(
        self,
        build: QueuedBuild,
        store: Store,
        resolved: BasicDerivation,
        original_drv_path: StorePath,
    ) -> None:
        """Serialize the resolved derivation, upload to both stores, and
        update build.request.
        """
        resolved_aterm = _unparse_basic_derivation(resolved, mask_outputs=False)
        drv_name = _nix_drv_name(original_drv_path)
        name_for_add = drv_name + ".drv"

        async def provide_resolved_drv(writer):
            fw = writer.framed()
            data = resolved_aterm.encode("utf-8")
            fw.write(data)
            await fw.finalize()

        resolved_drv_path: StorePath | None = None

        async def upload(target_store: Store) -> StorePath | None:
            add_req = AddToStoreRequest(
                path_name=name_for_add,
                cam="text:sha256",
                references=resolved.input_srcs,
                repair=0,
                async_provider=provide_resolved_drv,
            )
            try:
                resp = await add_req.execute(target_store, suppress_last=True)
                if resp.info is not None:
                    target_store.tracker.add_known_path(resp.info.path)
                    target_store.add_path_info(resp.info)
                    log.debug(
                        "resolved_drv_added_to_store",
                        build_id=build.build_id,
                        store_id=target_store.store_id,
                        resolved_drv_path=resp.info.path,
                    )
                    return resp.info.path
            except (BackendError, OSError, ConnectionError):
                log.warning(
                    "resolved_drv_add_to_store_failed",
                    build_id=build.build_id,
                    store_id=target_store.store_id,
                    exc_info=True,
                )
            return None

        targets = {self.local_store, store}
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(upload(s)) for s in targets]

        for t in tasks:
            path = t.result()
            if path is not None and resolved_drv_path is None:
                resolved_drv_path = path

        if resolved_drv_path is None:
            log.error("resolve_add_failed", build_id=build.build_id)
            return

        build.request.drv_path = resolved_drv_path
        build.request.derivation = resolved

    def _populate_required_paths(
        self,
        build: QueuedBuild,
        resolved: BasicDerivation,
        output_paths: StorePathSet,
    ) -> None:
        """Add resolved drv, output paths, input_srcs, and derivation
        outputs to build.required_paths.
        """
        build.required_paths[build.request.drv_path] = UnkeyedValidPathInfo()
        for p in output_paths:
            if p not in build.required_paths:
                build.required_paths[p] = UnkeyedValidPathInfo()
        for inp in resolved.input_srcs:
            sp = StorePath(inp)
            if sp not in build.required_paths:
                build.required_paths[sp] = UnkeyedValidPathInfo()
        for o in resolved.outputs.values():
            if o.path:
                sp = StorePath(o.path)
                if sp not in build.required_paths:
                    build.required_paths[sp] = UnkeyedValidPathInfo()
