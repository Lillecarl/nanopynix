from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile

import pytest
from _bench_utils import bench, bench_worker, bump_pipe_buf, run_bench
from anyio import Path
from grpclib.client import Channel
from grpclib_transports.protocol import make_config
from grpclib_transports.stdio import StdioChannel

PAYLOAD_CASES = {
    "64KiB": (os.urandom(64 * 1024), 30),
    "1MiB": (os.urandom(1024 * 1024), 5),
    "8MiB": (os.urandom(8 * 1024 * 1024), 2),
}


@pytest.mark.parametrize("payload_label", PAYLOAD_CASES)
def test_stdio_payload_sweep(payload_label: str) -> None:
    async def run():
        payload, count = PAYLOAD_CASES[payload_label]
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
            await bench_worker(f"stdio ({payload_label}, p=1)", payload, count, channel)
        finally:
            await channel.aclose()
            proc.kill()
            if proc.stderr is not None:
                await asyncio.wait_for(proc.stderr.read(), timeout=3)
            await proc.wait()

    run_bench(f"stdio ({payload_label}, p=1)", run())


@pytest.mark.parametrize("payload_label", PAYLOAD_CASES)
def test_unixproc_payload_sweep(payload_label: str) -> None:
    async def run():
        payload, count = PAYLOAD_CASES[payload_label]
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
            await bench(f"unixproc ({payload_label}, p=1)", payload, count, channel)
        finally:
            if channel is not None:
                channel.close()
            proc.kill()
            if proc.stderr is not None:
                await asyncio.wait_for(proc.stderr.read(), timeout=3)
            await proc.wait()
            with contextlib.suppress(OSError):
                await Path(sock).unlink()

    run_bench(f"unixproc ({payload_label}, p=1)", run())
