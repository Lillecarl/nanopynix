"""Common protocol implementations shared by the runnable examples."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
import greeter.greeter.worker as worker_grpc

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class Greeter(server_grpc.GreeterBase):
    @override
    async def say_hello(self, message: common_pb2.HelloRequest) -> common_pb2.HelloReply:
        return common_pb2.HelloReply(message=f"Hello, {message.name}!")

    @override
    async def upload(self, messages: AsyncIterator[common_pb2.HelloRequest]) -> common_pb2.HelloReply:
        total = 0
        async for request in messages:
            total += len(request.payload)
        return common_pb2.HelloReply(message=f"Uploaded {total} bytes")


class WorkerGreeter(worker_grpc.GreeterWorkerBase):
    @override
    async def say_hello(self, message: common_pb2.HelloRequest) -> common_pb2.HelloReply:
        return common_pb2.HelloReply(message=f"Hello, {message.name}!")

    @override
    async def upload(self, messages: AsyncIterator[common_pb2.HelloRequest]) -> common_pb2.HelloReply:
        total = 0
        async for request in messages:
            total += len(request.payload)
        return common_pb2.HelloReply(message=f"Uploaded {total} bytes")


class GreeterManager(worker_grpc.GreeterManagerBase):
    @override
    async def lookup(self, message: common_pb2.ManagerLookupRequest) -> common_pb2.ManagerLookupReply:
        return common_pb2.ManagerLookupReply(value=f"manager:{message.key}")


def worker_services() -> list[WorkerGreeter]:
    """The worker payload, here rather than in the script that spawns it.

    The forkserver pickles this by name, and the child resolves that name by
    importing the module. A factory defined in the ``__main__`` of the script
    has no module the child can import: see ``main_module_not_reexecuted``,
    which keeps the child from running that script a second time to make one.
    """
    return [WorkerGreeter()]
