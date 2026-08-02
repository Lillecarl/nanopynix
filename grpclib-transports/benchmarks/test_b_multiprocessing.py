from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from _bench_utils import LARGE_COUNT, LARGE_PAYLOAD, SMALL_COUNT, SMALL_PAYLOAD, bench_worker, run_bench
from grpclib_transports.example.server import WorkerGreeter
from grpclib_transports.multiprocessing import (
    MultiprocessingPipeEndpoint,
    multiprocessing_pipe_pair,
)
from grpclib_transports.pipes import pipe_streams_from_fds
from grpclib_transports.protocol import serve_h2


def _serve_multiprocessing_worker(endpoint: MultiprocessingPipeEndpoint) -> None:
    async def run() -> None:
        reader, _writer, transport = await pipe_streams_from_fds(
            os.dup(endpoint.read_connection.fileno()),
            os.dup(endpoint.write_connection.fileno()),
            transport_name="multiprocessing-worker",
        )
        endpoint.close_connections()
        await serve_h2([WorkerGreeter()], reader, transport)

    asyncio.run(run())


async def _stop_process(proc: Any) -> None:
    if proc.is_alive():
        proc.terminate()
        await asyncio.to_thread(proc.join, 3)
    if proc.is_alive():
        proc.kill()
        await asyncio.to_thread(proc.join, 3)


@pytest.mark.parametrize("parallelism", [1, 2, 4, 8])
def test_multiprocessing(parallelism: int) -> None:
    async def run():
        pair = multiprocessing_pipe_pair()
        proc = pair.context.Process(
            target=_serve_multiprocessing_worker,
            args=(pair.child,),
        )
        proc.start()
        pair.close_child_connections()

        channel = await pair.parent.open_channel()
        pair.close_parent_connections()
        try:
            await bench_worker(
                f"multiprocessing (small, p={parallelism})",
                SMALL_PAYLOAD,
                SMALL_COUNT,
                channel,
                parallelism=parallelism,
            )
            await bench_worker(
                f"multiprocessing (large, p={parallelism})",
                LARGE_PAYLOAD,
                LARGE_COUNT,
                channel,
                parallelism=parallelism,
            )
        finally:
            await channel.aclose()
            await _stop_process(proc)

    run_bench(f"multiprocessing (p={parallelism})", run())
