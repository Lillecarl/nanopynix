from __future__ import annotations

import asyncio
import contextlib
import os
import statistics
import sys
import tempfile
import time
from typing import Any

import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
import greeter.greeter.worker as worker_grpc
import pytest
from _bench_utils import BENCH_SAMPLES, bump_pipe_buf, report_bench, run_bench
from anyio import Path
from grpclib.client import Channel
from grpclib_transports.protocol import DEFAULT_TUNING, make_config
from grpclib_transports.stdio import StdioChannel
from grpclib_transports.transfer import iter_chunks

TOTAL_SIZE = 8 * 1024 * 1024
UPLOAD_COUNT = 2
UPLOAD_DATA = os.urandom(TOTAL_SIZE)


def _upload_messages() -> list[common_pb2.HelloRequest]:
    return [
        common_pb2.HelloRequest(name="chunk", payload=chunk)
        for chunk in iter_chunks(UPLOAD_DATA, DEFAULT_TUNING.transfer_chunk_size)
    ]


async def _upload_once(stub: Any) -> None:
    response = await stub.upload(_upload_messages())
    assert response.message == f"Uploaded {TOTAL_SIZE} bytes"


async def _bench_upload(label: str, channel: Any, *, worker: bool = False) -> None:
    stub = worker_grpc.GreeterWorkerStub(channel) if worker else server_grpc.GreeterStub(channel)
    await _upload_once(stub)

    samples: list[float] = []
    for _ in range(BENCH_SAMPLES):
        start = time.monotonic()
        for _ in range(UPLOAD_COUNT):
            await _upload_once(stub)
        samples.append(time.monotonic() - start)

    report_bench(
        label,
        UPLOAD_COUNT,
        statistics.median(samples),
        TOTAL_SIZE,
        samples,
    )


@pytest.mark.parametrize("transport", ["stdio", "unixproc"])
def test_streaming_upload_8mib(transport: str) -> None:
    async def run_stdio():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "grpclib_transports",
            "server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        bump_pipe_buf(proc)
        channel = StdioChannel(proc.stdout, proc.stdin)
        try:
            await _bench_upload("stdio (stream8MiB, p=1)", channel, worker=True)
        finally:
            await channel.aclose()
            proc.kill()
            if proc.stderr is not None:
                await asyncio.wait_for(proc.stderr.read(), timeout=3)
            await proc.wait()

    async def run_unixproc():
        fd, sock = tempfile.mkstemp(suffix=".sock")
        os.close(fd)
        await Path(sock).unlink()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "grpclib_transports",
            "server",
            "--unix-path",
            sock,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        channel = None
        try:
            for _ in range(100):
                if await Path(sock).exists():
                    break
                await asyncio.sleep(0.01)
            channel = Channel(path=sock, config=make_config())
            await _bench_upload("unixproc (stream8MiB, p=1)", channel)
        finally:
            if channel is not None:
                channel.close()
            proc.kill()
            if proc.stderr is not None:
                await asyncio.wait_for(proc.stderr.read(), timeout=3)
            await proc.wait()
            with contextlib.suppress(OSError):
                await Path(sock).unlink()

    if transport == "stdio":
        run_bench("stdio (stream8MiB, p=1)", run_stdio())
    else:
        run_bench("unixproc (stream8MiB, p=1)", run_unixproc())
