"""
Build planner for decomposing high-level build requests into derivations.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from ..derived_path import DerivedPath
from ..drv_parser import read_drv_file, to_basic_derivation
from ..protocol import Op, op_log
from .base import Uint64Response
from .builds import (
    BuildDerivationRequest,
    BuildDerivationResponse,
    BuildPathsRequest,
    BuildPathsWithResultsRequest,
    KeyedBuildResult,
    KeyedBuildResultsResponse,
)
from .queries import QueryMissingRequest

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..scheduler import Scheduler
    from ..store import Store

log = structlog.get_logger(__name__)


async def plan_and_execute_build_paths(
    request: BuildPathsRequest,
    store: Store,
    scheduler: Scheduler,
    client: ClientConn,
) -> Uint64Response:
    """Decompose and execute a BuildPaths request."""
    op_log("BuildPaths").debug("BuildPaths len(paths)=%d", len(request.derived_paths))
    decomposed = await decompose_build_paths(request, store, scheduler, client)

    if not decomposed:
        return Uint64Response(value=0)  # nothing to build

    # Await all futures
    futures = [f for _, _, f in decomposed]
    responses = await asyncio.gather(*futures)

    # Any failure → overall failure
    for resp in responses:
        if isinstance(resp, BuildDerivationResponse):
            if resp.result.status != 0:
                return Uint64Response(value=1)

    return Uint64Response(value=0)


async def plan_and_execute_build_paths_with_results(
    request: BuildPathsWithResultsRequest,
    store: Store,
    scheduler: Scheduler,
    client: ClientConn,
) -> KeyedBuildResultsResponse:
    """Decompose and execute a BuildPathsWithResults request."""
    op_log("BuildPathsWithResults").debug(
        "build_paths_with_results_decomposed",
        num_derivations=len(request.derived_paths),
    )
    decomposed = await decompose_build_paths(request, store, scheduler, client)

    if not decomposed:
        return KeyedBuildResultsResponse(results=[])

    # Await all futures
    futures = [f for _, _, f in decomposed]
    responses = await asyncio.gather(*futures)

    # Compose KeyedBuildResults from individual BuildDerivationResponses
    keyed_results: list[KeyedBuildResult] = []
    for (dp, _, _), resp in zip(decomposed, responses):
        if isinstance(resp, BuildDerivationResponse):
            keyed_results.append(
                KeyedBuildResult(
                    derived_path=dp,
                    result=resp.result,
                )
            )
            if resp.result.status not in (0, 1, 2):
                log.warning(
                    "unexpected_build_paths_with_results_status",
                    status=resp.result.status,
                    error_msg=resp.result.error_msg,
                )
            if resp.result.status != 0 and resp.result.error_msg and client:
                from ..stderr import StderrNext

                client.queue.put_nowait(
                    StderrNext(text=f"pynixd: {resp.result.error_msg}\n")
                )

    return KeyedBuildResultsResponse(results=keyed_results)


async def decompose_build_paths(
    request: BuildPathsRequest | BuildPathsWithResultsRequest,
    store: Store,
    scheduler: Scheduler,
    client: ClientConn,
) -> list[tuple[str, set[str], asyncio.Future[BuildDerivationResponse]]]:
    """Decompose BuildPaths into individual BuildDerivation requests.

    Returns list of (derived_path, output_names, future) tuples.
    """

    # Query which drvs actually need building
    missing_resp = await store.execute(
        QueryMissingRequest(derived_paths=request.derived_paths)
    )

    results: list[tuple[str, set[str], asyncio.Future[BuildDerivationResponse]]] = []

    # Resolve all builds first so we can batch-discover input paths
    resolved: list[tuple[str, set[str], BuildDerivationRequest]] = []
    all_input_srcs: set[str] = set()

    for dp in (DerivedPath(p) for p in missing_resp.will_build):
        try:
            parsed = dp.to_derivation(store.store_path)
        except FileNotFoundError:
            log.warning("drv_read_failed", drv_path=dp.drv_path)
            continue

        basic = to_basic_derivation(parsed, store.store_path)
        drv_request = BuildDerivationRequest(
            drv_path=dp.drv_path,
            derivation=basic,
            build_mode=request.build_mode,
        )
        resolved.append((str(dp), dp.output_names, drv_request))
        all_input_srcs.update(basic.input_srcs)

    # Discover paths that exist on the local store but aren't tracked.
    unknown = all_input_srcs - store.known_paths
    if unknown:
        valid = await store.query_valid_paths(unknown)
        store.add_known_paths(valid, update_regtime=False)

    for dp, output_names, drv_request in resolved:
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
        set(request.derivation.input_srcs),
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
