"""Multiprocessing transport — forkserver worker speaks gRPC over OS pipes.

Run with::

    python docs/examples/multiprocessing_example.py
"""

from __future__ import annotations

import asyncio

import greeter.greeter.common as common_pb2
import greeter.greeter.worker as worker_grpc
from grpclib_transports import Server
from services import WorkerGreeter


def worker_services() -> list[WorkerGreeter]:
    return [WorkerGreeter()]


async def main() -> None:
    async with Server() as server:
        workers = server.endpoint([]).for_workers()

        async with workers.multiprocessing_channels(
            worker_services,
            client_factory=worker_grpc.GreeterWorkerStub,
            preload=["greeter"],
            max_concurrency=1,
        ) as pool:
            stub = pool[0].client
            response = await stub.say_hello(common_pb2.HelloRequest(name="Multiprocessing"))
            assert response.message == "Hello, Multiprocessing!"
            print(f"Greeter replied: {response.message}")


if __name__ == "__main__":
    asyncio.run(main())
