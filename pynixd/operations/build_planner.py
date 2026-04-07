"""
Build planner for decomposing high-level build requests into derivations.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from ..derived_path import DerivedPath
from ..drv_parser import read_drv_file, to_basic_derivation
from ..protocol import Op
from ..store_path import StorePath
from .builds import (
    BuildDerivationRequest,
    BuildDerivationResponse,
    BuildPathsRequest,
    BuildPathsWithResultsRequest,
)
from .queries import QueryMissingRequest

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..scheduler import Scheduler
    from ..store import Store

log = structlog.get_logger(__name__)


async def decompose_build_paths(
    request: BuildPathsRequest | BuildPathsWithResultsRequest,
    store: Store,
    scheduler: Scheduler,
    client: ClientConn,
) -> list[tuple[DerivedPath, set[str], asyncio.Future[BuildDerivationResponse]]]:
    """Decompose BuildPaths into individual BuildDerivation requests.

    Returns list of (derived_path, output_names, future) tuples.
    """

    # Query which drvs actually need building
    missing_resp = await store.execute(
        QueryMissingRequest(derived_paths=request.derived_paths)
    )

    results: list[
        tuple[DerivedPath, set[str], asyncio.Future[BuildDerivationResponse]]
    ] = []

    # Resolve all builds first so we can batch-discover input paths
    resolved: list[tuple[DerivedPath, set[str], BuildDerivationRequest]] = []
    all_input_srcs: set[StorePath] = set()

    for dp in (DerivedPath(p) for p in missing_resp.will_build):
        try:
            parsed = dp.to_derivation(store.store_path)
        except FileNotFoundError:
            log.warning("drv_read_failed", drv_path=dp.drv_path)
            continue

        basic = to_basic_derivation(parsed, store.store_path)
        drv_request = BuildDerivationRequest(
            drv_path=StorePath(dp.drv_path),
            derivation=basic,
            build_mode=request.build_mode,
        )
        resolved.append((dp, dp.output_names, drv_request))
        all_input_srcs.update(basic.input_srcs)

    # Discover paths that exist on the local store but aren't tracked.
    unknown = all_input_srcs - store.known_paths
    if unknown:
        valid = await store.query_valid_paths(unknown)
        store.add_known_paths(valid, update_regtime=False)

    for dp, output_names, drv_request in resolved:
        # Expand input_srcs to full closure, matching Nix's behavior
        # when delegating to remote builders.
        drv_request.derivation.input_srcs = await store.compute_closure(
            drv_request.derivation.input_srcs
        )

        future = await enqueue_build_derivation(drv_request, store, scheduler, client)
        results.append((dp, output_names, future))

    return results


async def enqueue_build_derivation(
    request: BuildDerivationRequest,
    store: Store,
    scheduler: Scheduler,
    client: ClientConn,
) -> asyncio.Future[BuildDerivationResponse]:
    """Enqueue a single BuildDerivation request."""
    # Enrich with .drv metadata (e.g. _is_dynamic) if not already set
    enrich_derivation(request, store)

    build_id, future = await scheduler.enqueue(
        Op.BuildDerivation,
        request,
        client,
        request.derivation.input_srcs,
        platform=request.derivation.platform,
    )
    log.info(
        "build_derivation_enqueued",
        build_id=build_id,
        drv_path=request.drv_path,
    )

    return future


def enrich_derivation(request: BuildDerivationRequest, store: Store) -> None:
    """Set is_dynamic from the .drv file on disk."""
    store_path = store.store_path
    if not store_path:
        return
    try:
        parsed = read_drv_file(store_path, request.drv_path)
        request.derivation.is_dynamic = parsed.is_dynamic
    except FileNotFoundError:
        log.debug("drv_enrich_not_found", drv_path=request.drv_path)
    except Exception:
        log.debug("drv_enrich_failed", drv_path=request.drv_path, exc_info=True)
