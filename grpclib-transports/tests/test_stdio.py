from __future__ import annotations

import asyncio
import sys

import greeter.greeter.common as common_pb2
import greeter.greeter.worker as worker_grpc
from grpclib_transports import Server
from grpclib_transports.stdio import stdio_worker

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
