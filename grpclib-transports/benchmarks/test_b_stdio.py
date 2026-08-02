from __future__ import annotations

import asyncio
import sys

import pytest
from _bench_utils import LARGE_COUNT, LARGE_PAYLOAD, SMALL_COUNT, SMALL_PAYLOAD, bench_worker, bump_pipe_buf, run_bench
from grpclib_transports.stdio import StdioChannel


@pytest.mark.parametrize("parallelism", [1, 2, 4, 8])
def test_stdio(parallelism: int) -> None:
    async def run():
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
            await bench_worker(
                f"stdio (small, p={parallelism})", SMALL_PAYLOAD, SMALL_COUNT, channel, parallelism=parallelism
            )
            await bench_worker(
                f"stdio (large, p={parallelism})", LARGE_PAYLOAD, LARGE_COUNT, channel, parallelism=parallelism
            )
        finally:
            await channel.aclose()
            proc.kill()
            serr = await asyncio.wait_for(proc.stderr.read(), timeout=3) if proc.stderr else b""
            if serr:
                pass

    run_bench(f"stdio (p={parallelism})", run())
