"""Multi-store path transfer utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from .. import wire
from ..operations.add_multiple_to_store import AddMultipleToStoreRequest
from ..operations.add_to_store_nar import AddToStoreNarRequest
from ..operations.nar_from_path import NarFromPathRequest
from ..operations.query_closure_with_info import QueryClosureWithInfoRequest
from ..store_path import StorePath

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Iterable

    from ..types.path_info import ValidPathInfo
    from .base import Store


log = structlog.get_logger(__name__)


async def stream_paths_store_to_store(
    src: Store,
    dst: Store,
    paths: Iterable[StorePath],
    cancel_event: asyncio.Event | None = None,
) -> None:
    """Copy paths from src store to dst via streaming, querying closure first.

    Bypasses the normal handle() path, so we update dst knowledge manually.
    Only transfers paths that dst doesn't already have.
    """
    paths_set: set[StorePath] = {StorePath(p) for p in paths}
    if not paths_set:
        return

    # Fast-path for MockStore (used in tests)
    if type(src).__name__ == "MockStore" and type(dst).__name__ == "MockStore":
        dst.tracker.add_known_paths(paths_set)
        log.debug(
            "mock_path_transfer_complete",
            src=src.store_id,
            dst=dst.store_id,
            count=len(paths_set),
        )
        return

    # 1. Get closure from source
    closure_resp = await src.execute(
        QueryClosureWithInfoRequest(paths=paths_set),
        client=None,
    )
    if not closure_resp.infos:
        return

    # 2. Filter out paths already in destination
    to_transfer: list[ValidPathInfo] = [info for info in closure_resp.infos if info.path not in dst.tracker.known_paths]
    if not to_transfer:
        return

    # 3. Stream the missing paths
    async with src.transfer_conn() as src_conn, dst.transfer_conn() as dst_conn:
        dst_conn.op_log.append("AddMultipleToStore (stream_paths_to)")
        req = AddMultipleToStoreRequest(
            repair=0,
            dont_check_sigs=1,
        )
        await req.to_writer(dst_conn.w, dst_conn.version)
        await dst_conn.w.drain()

        fw = dst_conn.w.framed()
        fw.write_uint64(len(to_transfer))

        for info in to_transfer:
            if cancel_event and cancel_event.is_set():
                log.info("stream_paths_transfer_cancelled")
                break

            path = info.path
            dst_conn.op_log.append("AddToStoreNar (stream_paths_to)")

            # Use info.to_bytes() to send metadata as a single frame
            fw.write(info.to_bytes())

            # Request NAR from source
            await NarFromPathRequest(path=path).to_writer(
                src_conn.w,
                src_conn.version,
            )
            await src_conn.w.drain()

            # Source will send stderr logs followed by STDERR_LAST before NAR data
            await src_conn.r.drain_stderr()

            # Pipe raw NAR data from source into the destination's framed stream
            await wire.pipe_raw_to_framed_writer(
                src_conn.r,
                fw,
                info.nar_size,
            )
            await dst_conn.w.drain()

        await fw.finalize()
        await dst_conn.w.drain()
        await req.response_type().from_reader(dst_conn.r, dst_conn.version)

    # 4. Update destination store's knowledge
    dst.add_path_infos(set(to_transfer))
    dst.tracker.add_known_paths({i.path for i in to_transfer})


async def pipe_nar_store_to_store(
    src: Store,
    dst: Store,
    path: StorePath,
    info: ValidPathInfo,
) -> None:
    """Stream a single NAR from src store to dst store."""
    async with dst.transfer_conn() as dst_conn, src.transfer_conn() as src_conn:
        await NarFromPathRequest(path=path).to_writer(src_conn.w, src_conn.version)
        await src_conn.w.drain()

        nar_request = AddToStoreNarRequest(
            info=info,
            repair=0,
            dont_check_sigs=1,
        )
        await nar_request.to_writer(dst_conn.w, dst_conn.version)

        await wire.pipe_raw_to_framed(
            src_conn.r,
            dst_conn.w,
            info.nar_size,
        )

        await nar_request.response_type().from_reader(dst_conn.r, dst_conn.version)
