from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import statistics
import time
import traceback
from pathlib import Path
from typing import Any

import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
import greeter.greeter.worker as worker_grpc
from grpclib_transports.protocol import DEFAULT_TUNING
from grpclib_transports.stdio import bump_subprocess_pipe_buffers

logging.getLogger("h2").setLevel(logging.WARNING)
logging.getLogger("asyncssh").setLevel(logging.WARNING)


def bump_pipe_buf(proc: asyncio.subprocess.Process) -> None:
    """Increase kernel pipe buffer size on subprocess pipes."""
    bump_subprocess_pipe_buffers(proc, tuning=DEFAULT_TUNING)


SMALL_COUNT = 200
LARGE_COUNT = 5
LARGE_SIZE = 1024 * 1024
SMALL_PAYLOAD = b""
LARGE_PAYLOAD = os.urandom(LARGE_SIZE)

DUMP_DIR = Path.cwd() / ".bench-dumps"
DUMP_DIR.mkdir(parents=True, exist_ok=True)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer") from e
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


TIMEOUT = _env_int("GRPCLAB_BENCH_TIMEOUT", 30)
BENCH_SAMPLES = _env_int("GRPCLAB_BENCH_SAMPLES", 3)
STARTUP_COUNT = _env_int("GRPCLAB_BENCH_STARTUP_COUNT", 3)
PROFILE_BENCHMARKS = os.environ.get("GRPCLAB_BENCH_PROFILE") == "1"

bench_results: list[dict[str, Any]] = []
latency_results: list[dict[str, Any]] = []
dump_paths: list[Path] = []


def _sample_rate(count: float, elapsed: float, payload_size: float) -> float:
    if payload_size:
        return count * payload_size / elapsed / (1024 * 1024)
    return count / elapsed


def report_bench(label: str, count: int, elapsed: float, payload_size: int, samples: list[float]) -> None:
    msgs_per_sec = count / elapsed
    total_bytes = count * payload_size
    mb_per_sec = total_bytes / elapsed / (1024 * 1024)
    if payload_size:
        pass

    m = re.match(r"(\w+) \((\w+), p=(\d+)\)", label)
    if m:
        bench_results.append(
            {
                "transport": m.group(1),
                "type": m.group(2),
                "parallelism": int(m.group(3)),
                "count": count,
                "elapsed": elapsed,
                "payload_size": payload_size,
                "msgs_per_sec": msgs_per_sec,
                "mb_per_sec": mb_per_sec,
                "sample_rates": [_sample_rate(count, sample, payload_size) for sample in samples],
            }
        )


def _report_latency(label: str, count: int, elapsed: float, samples: list[float]) -> None:
    m = re.match(r"(\w+) \((\w+)\)", label)
    if m:
        latency_results.append(
            {
                "transport": m.group(1),
                "type": m.group(2),
                "count": count,
                "elapsed": elapsed,
                "ms_per_op": elapsed / count * 1000,
                "sample_ms_per_op": [sample / count * 1000 for sample in samples],
            }
        )


def _dump_tasks(label: str, loop: asyncio.AbstractEventLoop) -> Path:
    """Dump all asyncio task stacks to a file, return the file path."""
    safe = re.sub(r"[^\w.]", "_", label)
    path = DUMP_DIR / f"{safe}_tasks.txt"
    buf = io.StringIO()
    buf.write(f"=== Task stack dump for '{label}' at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")
    tasks = asyncio.all_tasks(loop)
    buf.write(f"Total tasks: {len(tasks)}\n\n")
    for i, task in enumerate(sorted(tasks, key=lambda t: t.get_name())):
        buf.write(f"--- Task {i}: {task.get_name()} done={task.done()} ---\n")
        task.print_stack(file=buf)
        buf.write("\n")
    content = buf.getvalue()
    with path.open("w") as f:
        f.write(content)
    # Print a brief summary to stderr/stdout
    # Print the first 60 lines inline so it shows up in -s mode
    for _line in content.splitlines()[:60]:
        pass
    dump_paths.append(path)
    return path


def _dump_profile(label: str, path_to_timeline: str) -> Path:
    """Write the pyinstrument pathToTimeline to a text file, return the file path."""
    safe = re.sub(r"[^\w.]", "_", label)
    path = DUMP_DIR / f"{safe}_profile.txt"
    with path.open("w") as f:
        f.write(path_to_timeline)
    dump_paths.append(path)
    return path


async def _bench_warmup(stub: Any, req: Any, parallelism: int = 1) -> None:
    if parallelism == 1:
        await stub.say_hello(req)
    else:
        warmup = [asyncio.create_task(stub.say_hello(req)) for _ in range(parallelism)]
        await asyncio.gather(*warmup)


async def _bench_once(stub: Any, req: Any, count: int, parallelism: int = 1) -> float:
    if parallelism == 1:
        start = time.perf_counter()
        for _ in range(count):
            await stub.say_hello(req)
        return time.perf_counter() - start

    q: asyncio.Queue[None] = asyncio.Queue()
    for _ in range(count):
        q.put_nowait(None)

    async def worker():
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                return
            await stub.say_hello(req)

    start = time.perf_counter()
    workers = [asyncio.create_task(worker()) for _ in range(parallelism)]
    await asyncio.gather(*workers)
    return time.perf_counter() - start


async def bench(label: str, payload: bytes, count: int, channel: Any, parallelism: int = 1) -> None:
    stub = server_grpc.GreeterStub(channel)
    req = common_pb2.HelloRequest(name="bench", payload=payload)
    await _bench_warmup(stub, req, parallelism=parallelism)
    samples = [await _bench_once(stub, req, count, parallelism=parallelism) for _ in range(BENCH_SAMPLES)]
    elapsed = statistics.median(samples)
    report_bench(label, count, elapsed, len(payload), samples)


async def bench_worker(label: str, payload: bytes, count: int, channel: Any, parallelism: int = 1) -> None:
    stub = worker_grpc.GreeterWorkerStub(channel)
    req = common_pb2.HelloRequest(name="bench", payload=payload)
    await _bench_warmup(stub, req, parallelism=parallelism)
    samples = [await _bench_once(stub, req, count, parallelism=parallelism) for _ in range(BENCH_SAMPLES)]
    elapsed = statistics.median(samples)
    report_bench(label, count, elapsed, len(payload), samples)


async def bench_lifecycle(label: str, count: int, operation: Any) -> None:
    samples: list[float] = []
    for _ in range(BENCH_SAMPLES):
        start = time.perf_counter()
        for _ in range(count):
            await operation()
        samples.append(time.perf_counter() - start)
    elapsed = statistics.median(samples)
    _report_latency(label, count, elapsed, samples)


async def _runner_with_timeout(coro: Any, label: str, loop: asyncio.AbstractEventLoop) -> None:
    """
    Run coro with a manual timeout. On timeout, dump task stacks *before*
    cancelling anything, so we capture the true deadlock state.
    """
    main_task = asyncio.ensure_future(coro, loop=loop)
    timed_out = False

    def _on_timeout():
        nonlocal timed_out
        timed_out = True
        _dump_tasks(label, loop)
        main_task.cancel()

    timer = loop.call_later(TIMEOUT, _on_timeout)
    try:
        await main_task
    except asyncio.CancelledError:
        if timed_out:
            raise TimeoutError from None
        raise
    finally:
        timer.cancel()


def _run_with_dump(label: str, coro_factory: Any) -> None:
    """
    Run a benchmark with full profiling support.

    coro_factory: a zero-arg callable that returns a fresh coroutine.
    We need a factory because we might need to re-create the coroutine.

    On success: dump pyinstrument profile.
    On timeout: dump all asyncio task stacks from the event loop *before*
    cancelling, so we capture the true deadlock state.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    profiler = None
    if PROFILE_BENCHMARKS:
        from pyinstrument import Profiler

        profiler = Profiler(interval=0.001)
        profiler.start()

    coro = coro_factory()
    wrapper = _runner_with_timeout(coro, label, loop)

    try:
        loop.run_until_complete(wrapper)
    except TimeoutError:
        raise  # already dumped tasks in _on_timeout
    except Exception:
        traceback.print_exc()
        _dump_tasks(label, loop)
        raise
    finally:
        if profiler is not None:
            profiler.stop()
            try:
                profile_text = profiler.output_text(unicode=True, color=False, show_all=True)
            except Exception:
                profile_text = "Failed to generate pyinstrument text output"
            _dump_profile(label, profile_text)

        # Cancel any remaining tasks
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def run_bench(label: str, coro: Any) -> None:
    """
    Run a benchmark with profiling and task-stack dump on timeout.
    Accepts a coroutine (not a factory) — wraps it for _run_with_dump.
    """
    _run_with_dump(label, lambda: coro)
