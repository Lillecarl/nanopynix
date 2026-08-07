from __future__ import annotations

import asyncio
import sys
from typing import Any

import anyio
import greeter.greeter.common as common_pb2
import greeter.greeter.worker as worker_grpc
from grpclib_transports import Server
from grpclib_transports.stdio import stdio_worker, stdio_worker_with_backchannel

_BACKCHANNEL_WORKER_CODE = """
from __future__ import annotations

import asyncio

import greeter.greeter.common as common_pb2
import greeter.greeter.worker as worker_grpc
from grpclib_transports.stdio import serve_stdio_with_backchannel


class WorkerThatCallsManager(worker_grpc.GreeterWorkerBase):
    def __init__(self, backchannel):
        self._backchannel = backchannel

    async def say_hello(self, message):
        lookup = await self._backchannel.call_unary(
            "/greeter.worker.GreeterManager/Lookup",
            common_pb2.ManagerLookupRequest(key=message.name),
            common_pb2.ManagerLookupReply,
        )
        return common_pb2.HelloReply(message=lookup.value)


def services(backchannel):
    return [WorkerThatCallsManager(backchannel)]


asyncio.run(serve_stdio_with_backchannel(services, max_concurrency=2))
"""


_WIRE_DEADLINE = 20.0
"""How long a call gets before the test calls the wire broken.

Generous against the work: one exec of the interpreter plus one unary call.
It bounds a hang, and it is not a performance assertion."""

_WORKER_THAT_WRITES_TO_THE_WIRE_DESCRIPTORS = """
from __future__ import annotations

import asyncio
import os
import sys

import greeter.greeter.common as common_pb2
import greeter.greeter.worker as worker_grpc
from grpclib_transports.stdio import serve_stdio_with_backchannel


class WorkerThatWritesToDescriptorOne(worker_grpc.GreeterWorkerBase):
    async def say_hello(self, message):
        # Every one of these went to the wire before the guard existed. The
        # first is the one no redirection of `sys.stdout` can reach.
        os.write(1, b"raw-descriptor-one\\n")
        print("python-level-stdout", flush=True)
        sys.stdout.write("replaced-stream\\n")
        sys.stdout.flush()
        # What descriptor 0 now is, reported back over the wire: a subprocess
        # that reads it must not be reading frames.
        return common_pb2.HelloReply(message=os.readlink("/proc/self/fd/0"))


def services(backchannel):
    return [WorkerThatWritesToDescriptorOne()]


asyncio.run(serve_stdio_with_backchannel(services, max_concurrency=2))
"""


class GreeterManager(worker_grpc.GreeterManagerBase):
    async def lookup(self, message: common_pb2.ManagerLookupRequest) -> common_pb2.ManagerLookupReply:
        return common_pb2.ManagerLookupReply(value=f"manager:{message.key}")


async def test_stdio_transport() -> None:
    async with stdio_worker(
        [sys.executable, "-m", "grpclib_transports", "server", "--stdio"],
        stderr=asyncio.subprocess.PIPE,
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        response = await stub.say_hello(common_pb2.HelloRequest(name="Stdio"))
        assert response.message == "Hello, Stdio!"


async def test_stdio_worker_can_call_parent_services() -> None:
    async with Server() as server:
        host = server.endpoint([GreeterManager()]).for_workers()

        async with host.stdio_channels(
            [sys.executable, "-c", _BACKCHANNEL_WORKER_CODE],
            client_factory=worker_grpc.GreeterWorkerStub,
            stderr=asyncio.subprocess.PIPE,
        ) as pool:
            stub = pool[0].client
            response = await stub.say_hello(common_pb2.HelloRequest(name="stdio"))

        assert response.message == "manager:stdio"


async def test_a_write_to_descriptor_one_reaches_stderr_and_not_the_wire() -> None:
    """The worker writes to descriptor 1 three ways, and the call still answers.

    ``serve_stdio`` used to redirect ``sys.stdout`` alone, which is a rebinding
    of a Python name. It cannot stop a C++ library, a C extension or a
    subprocess from writing to the descriptor, and every such byte becomes an
    HTTP/2 frame. ``take_wire_descriptors`` moves the wire off descriptor 1,
    and puts a duplicate of descriptor 2 there instead.

    Three assertions, and each one covers a different way to get this wrong:

    * The reply arrives, so the raw write did not corrupt the stream.
    * The reply names ``/dev/null``, so descriptor 0 is not the wire either. A
      subprocess that reads it takes frames the transport needed.
    * All three writes are on stderr, so the guard moved them rather than
      discarding them. Pointing descriptor 1 at the null device would keep the
      wire just as clean and lose every diagnostic message a worker prints.
    """
    started: list[Any] = []

    async with stdio_worker(
        [sys.executable, "-c", _WORKER_THAT_WRITES_TO_THE_WIRE_DESCRIPTORS],
        stderr=asyncio.subprocess.PIPE,
        on_process_start=started.append,
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        # A deadline, because the failure this guards against is a hang and
        # not an exception: the raw write desynchronises the HTTP/2 stream, and
        # the reply then never comes at all. Measured with the guard removed.
        with anyio.fail_after(_WIRE_DEADLINE):
            response = await stub.say_hello(common_pb2.HelloRequest(name="stdio"))

    assert response.message == "/dev/null"

    proc = started[0]
    assert proc.stderr is not None
    logged = (await proc.stderr.read()).decode()
    assert "raw-descriptor-one" in logged
    assert "python-level-stdout" in logged
    assert "replaced-stream" in logged


async def test_on_process_start_receives_the_worker_process() -> None:
    """The hook is the only way a caller reaches the process.

    The channel carries the wire and says nothing about the peer, so without
    this a caller cannot read the exit status, and an abort, a segmentation
    fault and an ordinary exit are one closed pipe.
    ``multiprocessing_worker`` takes the same hook for the same reason.
    """
    started: list[Any] = []

    async with stdio_worker(
        [sys.executable, "-m", "grpclib_transports", "server", "--stdio"],
        stderr=asyncio.subprocess.PIPE,
        on_process_start=started.append,
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        await stub.say_hello(common_pb2.HelloRequest(name="Stdio"))

    assert len(started) == 1
    assert isinstance(started[0].pid, int)
    assert started[0].pid > 0


async def test_a_stdio_worker_ends_itself_rather_than_dying_of_a_signal() -> None:
    """The parent waits for the child before it signals it.

    A worker does its own teardown after ``serve_h2`` returns, and the parent
    reached ``terminate()`` milliseconds after it closed the channel. So every
    healthy worker died of SIGTERM with its teardown unrun.
    ``_close_worker_process`` gives the child a grace period now, and the
    signal is the fallback it was meant to be. ``_stop_process`` in
    ``multiprocessing.py`` carries the same period, and
    ``test_a_worker_ends_itself_rather_than_dying_of_a_signal`` is this test
    for that transport.

    ``returncode`` is the whole assertion: 0 says the child ended itself, and
    -15 says the signal got there first.
    """
    started: list[Any] = []

    async with stdio_worker_with_backchannel(
        [sys.executable, "-c", _BACKCHANNEL_WORKER_CODE],
        [GreeterManager()],
        stderr=asyncio.subprocess.PIPE,
        on_process_start=started.append,
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        await stub.say_hello(common_pb2.HelloRequest(name="stdio"))

    assert [proc.returncode for proc in started] == [0]
