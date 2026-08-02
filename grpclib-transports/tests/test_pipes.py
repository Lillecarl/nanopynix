from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import os
from typing import Any

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
