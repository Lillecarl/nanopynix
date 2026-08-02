from __future__ import annotations

from typing import Any

import greeter.greeter.common as common_pb2
import greeter.greeter.worker as worker_grpc
from grpclib_transports import LogicalRpcPeer, Server, WorkerBackchannel, WorkerHost
from grpclib_transports.example.server import Greeter


async def _peer_factory(_channel: Any) -> LogicalRpcPeer:
    raise AssertionError("pool construction must not start workers")


class WorkerThatCallsManager(worker_grpc.GreeterWorkerBase):
    def __init__(self, backchannel: WorkerBackchannel) -> None:
        self._backchannel = backchannel

    async def say_hello(self, message: common_pb2.HelloRequest) -> common_pb2.HelloReply:
        lookup = await self._backchannel.call_unary(
            "/greeter.worker.GreeterManager/Lookup",
            common_pb2.ManagerLookupRequest(key=message.name),
            common_pb2.ManagerLookupReply,
        )
        return common_pb2.HelloReply(message=lookup.value)


def _worker_services_with_manager(backchannel: WorkerBackchannel) -> list[WorkerThatCallsManager]:
    return [WorkerThatCallsManager(backchannel)]


class GreeterManager(worker_grpc.GreeterManagerBase):
    async def lookup(self, message: common_pb2.ManagerLookupRequest) -> common_pb2.ManagerLookupReply:
        return common_pb2.ManagerLookupReply(value=f"manager:{message.key}")


async def test_server_for_workers_creates_worker_host() -> None:
    worker_service = Greeter()

    async with Server() as server:
        host = server.endpoint([worker_service]).for_workers()

        assert isinstance(host, WorkerHost)
        assert host.parent_services == (worker_service,)
        assert host.tuning is server.tuning


async def test_worker_host_creates_stdio_pool_with_count() -> None:
    async with Server() as server:
        host = server.endpoint([]).for_workers()

        pool = host.stdio_pool(
            ["python", "-m", "worker"],
            peer_factory=_peer_factory,
            count=3,
        )

        assert len(pool) == 0


async def test_multiprocessing_worker_can_call_parent_services() -> None:
    async with Server() as server:
        host = server.endpoint([GreeterManager()]).for_workers()

        async with host.multiprocessing_channels(
            _worker_services_with_manager,
            client_factory=worker_grpc.GreeterWorkerStub,
            preload=["greeter"],
        ) as pool:
            stub = pool[0].client
            response = await stub.say_hello(common_pb2.HelloRequest(name="alpha"))

        assert response.message == "manager:alpha"


async def test_multiprocessing_worker_pool_reports_started_processes() -> None:
    seen_pids: list[int] = []

    def on_process_start(proc: Any) -> None:
        seen_pids.append(proc.pid)

    async with Server() as server:
        host = server.endpoint([GreeterManager()]).for_workers()

        async with host.multiprocessing_channels(
            _worker_services_with_manager,
            client_factory=worker_grpc.GreeterWorkerStub,
            count=2,
            on_process_start=on_process_start,
            preload=["greeter"],
        ) as pool:
            response = await pool[0].client.say_hello(common_pb2.HelloRequest(name="alpha"))

    assert response.message == "manager:alpha"
    assert len(seen_pids) == 2
    assert all(isinstance(pid, int) and pid > 0 for pid in seen_pids)


async def test_inproc_worker_can_call_parent_services() -> None:
    async with Server() as server:
        host = server.endpoint([GreeterManager()]).for_workers()

        async with host.inproc_channels(
            _worker_services_with_manager,
            client_factory=worker_grpc.GreeterWorkerStub,
        ) as pool:
            response = await pool[0].client.say_hello(common_pb2.HelloRequest(name="alpha"))
            assert pool[0].id == "inproc-1"
            assert pool[0].metadata == {"transport": "inproc", "index": 0}

    assert response.message == "manager:alpha"
