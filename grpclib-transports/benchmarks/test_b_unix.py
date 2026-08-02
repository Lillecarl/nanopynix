from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile

import pytest
from _bench_utils import LARGE_COUNT, LARGE_PAYLOAD, SMALL_COUNT, SMALL_PAYLOAD, bench, run_bench
from anyio import Path
from grpclib.client import Channel
from grpclib.server import Server as GrpcServer
from grpclib_transports.example.server import Greeter
from grpclib_transports.protocol import make_config


@pytest.mark.parametrize("parallelism", [1, 2, 4, 8])
def test_unix(parallelism: int) -> None:
    async def run():
        fd, sock = tempfile.mkstemp(suffix=".sock")
        os.close(fd)
        await Path(sock).unlink()
        server = GrpcServer([Greeter()], config=make_config())
        await server.start(path=sock)
        try:
            channel = Channel(path=sock, config=make_config())
            await bench(f"unix (small, p={parallelism})", SMALL_PAYLOAD, SMALL_COUNT, channel, parallelism=parallelism)
            await bench(f"unix (large, p={parallelism})", LARGE_PAYLOAD, LARGE_COUNT, channel, parallelism=parallelism)
            channel.close()
        finally:
            server.close()
            await server.wait_closed()
            with contextlib.suppress(OSError):
                await Path(sock).unlink()

    run_bench(f"unix (p={parallelism})", run())


@pytest.mark.parametrize("parallelism", [1, 2, 4, 8])
def test_unix_subprocess(parallelism: int) -> None:
    async def run():
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
            await bench(
                f"unixproc (small, p={parallelism})", SMALL_PAYLOAD, SMALL_COUNT, channel, parallelism=parallelism
            )
            await bench(
                f"unixproc (large, p={parallelism})", LARGE_PAYLOAD, LARGE_COUNT, channel, parallelism=parallelism
            )
        finally:
            if channel is not None:
                channel.close()
            proc.kill()
            if proc.stderr is not None:
                await asyncio.wait_for(proc.stderr.read(), timeout=3)
            await proc.wait()
            with contextlib.suppress(OSError):
                await Path(sock).unlink()

    run_bench(f"unixproc (p={parallelism})", run())
