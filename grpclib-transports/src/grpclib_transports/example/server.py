from __future__ import annotations

import asyncio
import signal
import typing

import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
import greeter.greeter.worker as worker_grpc

from grpclib_transports.protocol import signal_stop
from grpclib_transports.server import Server

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator


class Greeter(server_grpc.GreeterBase):
    @typing.override
    async def say_hello(self, message: common_pb2.HelloRequest) -> common_pb2.HelloReply:
        return common_pb2.HelloReply(message=f"Hello, {message.name}!")

    @typing.override
    async def upload(self, messages: AsyncIterator[common_pb2.HelloRequest]) -> common_pb2.HelloReply:
        total = 0
        async for request in messages:
            total += len(request.payload)
        return common_pb2.HelloReply(message=f"Uploaded {total} bytes")


class WorkerGreeter(worker_grpc.GreeterWorkerBase):
    @typing.override
    async def say_hello(self, message: common_pb2.HelloRequest) -> common_pb2.HelloReply:
        return common_pb2.HelloReply(message=f"Hello, {message.name}!")

    @typing.override
    async def upload(self, messages: AsyncIterator[common_pb2.HelloRequest]) -> common_pb2.HelloReply:
        total = 0
        async for request in messages:
            total += len(request.payload)
        return common_pb2.HelloReply(message=f"Uploaded {total} bytes")


async def serve(path: str) -> None:
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: signal_stop(stop))

    async with Server() as server:
        await server.endpoint([Greeter()]).listen_unix(path)
        await stop
