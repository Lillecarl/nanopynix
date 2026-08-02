"""Unix domain socket transport — in-process server and client.

Run with::

    python docs/examples/unix_example.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile

import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
from anyio import Path
from grpclib_transports import Server, connect_unix
from services import Greeter


async def main() -> None:
    fd, sock = tempfile.mkstemp(suffix=".sock")
    os.close(fd)
    await Path(sock).unlink()
    channel = None
    try:
        async with Server() as server:
            await server.endpoint([Greeter()]).listen_unix(sock)
            channel = connect_unix(sock)
            try:
                stub = server_grpc.GreeterStub(channel)
                response = await stub.say_hello(common_pb2.HelloRequest(name="World"))
                assert response.message == "Hello, World!"
                print(f"Greeter replied: {response.message}")
            finally:
                channel.close()
                channel = None
    finally:
        if channel is not None:
            channel.close()
        with contextlib.suppress(OSError):
            await Path(sock).unlink()


if __name__ == "__main__":
    asyncio.run(main())
