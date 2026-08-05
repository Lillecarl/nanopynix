from __future__ import annotations

import asyncio
import contextlib
import functools
import multiprocessing as mp
import os
from collections.abc import Collection
from pathlib import Path
from typing import Any

import anyio
import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
import greeter.greeter.worker as worker_grpc
from grpclib_transports.example.server import Greeter, WorkerGreeter
from grpclib_transports.inproc import inproc_pipe_pair, inproc_worker
from grpclib_transports.multiprocessing import multiprocessing_pipe_pair, multiprocessing_worker
from grpclib_transports.pipes import (
    PipeChannel,
    pipe_streams_from_fds,
)
from grpclib_transports.protocol import serve_h2


def _worker_services() -> list[WorkerGreeter]:
    return [WorkerGreeter()]


async def _record_teardown(marker: str, services: Collection[Any]) -> None:
    """A ``child_teardown`` that leaves proof it ran, and what it received.

    Module-level, because the forkserver pickles this the way it pickles the
    factory. ``functools.partial`` carries the path: an environment variable
    would not arrive, because the forkserver copies the environment once, when
    it starts.

    A file, because the child has no other way to report to the parent at this
    point. Its channel is closed -- that is what makes the teardown run.
    """
    await anyio.Path(marker).write_text(f"{len(services)} {type(next(iter(services))).__name__}", encoding="utf-8")


async def _assert_pipe_round_trip(
    client_read_fd: int,
    client_write_fd: int,
    server_read_fd: int,
    server_write_fd: int,
) -> None:
    client_reader, client_writer, client_transport = await pipe_streams_from_fds(
        client_read_fd,
        client_write_fd,
        transport_name="client-pipe",
    )
    server_reader, _server_writer, server_transport = await pipe_streams_from_fds(
        server_read_fd,
        server_write_fd,
        transport_name="server-pipe",
    )
    server_task = asyncio.create_task(
        serve_h2([Greeter()], server_reader, server_transport),
        name="pipe-test-server",
    )
    channel = PipeChannel(
        client_reader,
        client_writer,
        transport=client_transport,
    )
    try:
        stub = server_grpc.GreeterStub(channel)
        response = await stub.say_hello(common_pb2.HelloRequest(name="Pipe"))
        assert response.message == "Hello, Pipe!"
    finally:
        await channel.aclose()
        server_transport.close()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task


async def test_raw_pipe_transport() -> None:
    server_to_client_read, server_to_client_write = os.pipe()
    client_to_server_read, client_to_server_write = os.pipe()

    await _assert_pipe_round_trip(
        client_read_fd=server_to_client_read,
        client_write_fd=client_to_server_write,
        server_read_fd=client_to_server_read,
        server_write_fd=server_to_client_write,
    )


async def test_multiprocessing_pipe_pair_uses_forkserver_context() -> None:
    assert mp.get_start_method(allow_none=True) is None
    pair = multiprocessing_pipe_pair(preload=["greeter"])
    assert pair.context.get_start_method() == "forkserver"
    assert mp.get_start_method(allow_none=True) is None
    try:
        await _assert_pipe_round_trip(
            client_read_fd=os.dup(pair.parent.read_connection.fileno()),
            client_write_fd=os.dup(pair.parent.write_connection.fileno()),
            server_read_fd=os.dup(pair.child.read_connection.fileno()),
            server_write_fd=os.dup(pair.child.write_connection.fileno()),
        )
    finally:
        pair.close_parent_connections()
        pair.close_child_connections()


async def test_inproc_pipe_pair() -> None:
    pair = inproc_pipe_pair()
    try:
        await _assert_pipe_round_trip(
            client_read_fd=os.dup(pair.parent.read_fd),
            client_write_fd=os.dup(pair.parent.write_fd),
            server_read_fd=os.dup(pair.child.read_fd),
            server_write_fd=os.dup(pair.child.write_fd),
        )
    finally:
        pair.close_parent_connections()
        pair.close_child_connections()


async def test_inproc_worker_context_manager_keeps_service_accessible() -> None:
    worker = WorkerGreeter()

    async with inproc_worker(lambda: [worker]) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        response = await stub.say_hello(common_pb2.HelloRequest(name="Worker"))

    assert response.message == "Hello, Worker!"


async def test_multiprocessing_worker_context_manager() -> None:
    async with multiprocessing_worker(_worker_services, preload=["greeter"]) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        response = await stub.say_hello(common_pb2.HelloRequest(name="Worker"))
        assert response.message == "Hello, Worker!"


async def test_multiprocessing_worker_reports_started_process() -> None:
    seen_pid: int | None = None

    def on_process_start(proc: Any) -> None:
        nonlocal seen_pid
        seen_pid = proc.pid

    async with multiprocessing_worker(
        _worker_services,
        on_process_start=on_process_start,
        preload=["greeter"],
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        response = await stub.say_hello(common_pb2.HelloRequest(name="Worker"))

    assert response.message == "Hello, Worker!"
    assert isinstance(seen_pid, int)
    assert seen_pid > 0


async def test_multiprocessing_worker_runs_its_child_teardown(tmp_path: Path) -> None:
    """The child gets a hook after ``serve_h2`` returns, and it runs.

    There was no hook at all, so a worker had nowhere to put the teardown it
    owns, and nothing it did after serving ever ran. ``child_teardown`` is
    that place. It runs inside the child's event loop, because a worker's
    teardown awaits the tasks it started there.

    The hook receives what the factory built, and not the tuple that was
    served: the backchannel service belongs to this transport, and the
    teardown is the worker's own. The marker records the count and the type,
    so this holds both halves.

    ``exitcode`` is asserted beside it. A hook that ran but pushed the child
    past ``_PROCESS_EXIT_GRACE`` would be signalled, and 0 says it was not.
    """
    marker = tmp_path / "teardown.marker"
    seen: list[Any] = []

    async with multiprocessing_worker(
        _worker_services,
        on_process_start=seen.append,
        preload=["greeter"],
        child_teardown=functools.partial(_record_teardown, str(marker)),
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        await stub.say_hello(common_pb2.HelloRequest(name="Worker"))

    assert marker.read_text(encoding="utf-8") == "1 WorkerGreeter"
    assert seen[0].exitcode == 0, f"the child was signalled rather than left to run its teardown: {seen[0].exitcode}"
