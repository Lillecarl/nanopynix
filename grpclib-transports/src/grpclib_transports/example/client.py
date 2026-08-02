from __future__ import annotations

from typing import Any

import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
import greeter.greeter.worker as worker_grpc

from grpclib_transports.client import connect_unix
from grpclib_transports.ssh import connect_ssh
from grpclib_transports.stdio import StdioChannel, stdio_streams


async def greet(channel: Any, name: str = "World") -> None:
    stub = server_grpc.GreeterStub(channel)
    request = common_pb2.HelloRequest(name=name)
    await stub.say_hello(request)


async def greet_worker(channel: Any, name: str = "World") -> None:
    stub = worker_grpc.GreeterWorkerStub(channel)
    request = common_pb2.HelloRequest(name=name)
    await stub.say_hello(request)


async def greet_unix(path: str, name: str = "World") -> None:
    async with connect_unix(path) as channel:
        await greet(channel, name)


async def greet_stdio(name: str = "World") -> None:
    reader, writer, transport = await stdio_streams()
    channel = StdioChannel(reader, writer, transport=transport)
    try:
        await greet_worker(channel, name)
    finally:
        channel.close()


async def greet_ssh(
    host: str = "127.0.0.1",
    port: int = 8022,
    username: str = "demo",
    password: str = "demo",
    name: str = "World",
) -> None:
    async with connect_ssh(
        host,
        port,
        username=username,
        password=password,
        known_hosts=None,
    ) as channel:
        await greet(channel, name)
