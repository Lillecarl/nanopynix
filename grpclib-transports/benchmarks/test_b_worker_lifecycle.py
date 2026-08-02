from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import greeter.greeter.common as common_pb2
import greeter.greeter.worker as worker_grpc
from _bench_utils import STARTUP_COUNT, bench_lifecycle, run_bench
from grpclib_transports.example.server import WorkerGreeter
from grpclib_transports.multiprocessing import (
    MultiprocessingPipeEndpoint,
    multiprocessing_pipe_pair,
)
from grpclib_transports.pipes import pipe_streams_from_fds
from grpclib_transports.protocol import serve_h2
from grpclib_transports.stdio import stdio_worker


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


async def _stdio_lifecycle() -> None:
    async with stdio_worker(
        [sys.executable, "-m", "grpclib_transports", "server", "--stdio"],
        stderr=asyncio.subprocess.DEVNULL,
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        response = await stub.say_hello(common_pb2.HelloRequest(name="Lifecycle"))
        if response.message != "Hello, Lifecycle!":
            raise RuntimeError(response.message)


async def _multiprocessing_lifecycle() -> None:
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
        stub = worker_grpc.GreeterWorkerStub(channel)
        response = await stub.say_hello(common_pb2.HelloRequest(name="Lifecycle"))
        if response.message != "Hello, Lifecycle!":
            raise RuntimeError(response.message)
    finally:
        await channel.aclose()
        await _stop_process(proc)


def test_stdio_worker_lifecycle():
    async def run() -> None:
        await bench_lifecycle(
            "stdio (lifecycle)",
            STARTUP_COUNT,
            _stdio_lifecycle,
        )

    run_bench("stdio lifecycle", run())


def test_multiprocessing_worker_lifecycle():
    async def run() -> None:
        await bench_lifecycle(
            "multiprocessing (lifecycle)",
            STARTUP_COUNT,
            _multiprocessing_lifecycle,
        )

    run_bench("multiprocessing lifecycle", run())
