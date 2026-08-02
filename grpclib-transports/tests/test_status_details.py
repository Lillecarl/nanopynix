"""``status_details_codec`` must reach both ends of every transport.

grpclib carries a ``GRPCError``'s ``details`` payload in the
``grpc-status-details-bin`` trailer, but only when a codec is configured --
and it degrades *silently* when one isn't: the server skips the trailer
(``grpclib/server.py``) and the client leaves ``details`` as ``None``
(``grpclib/client.py``). So a transport helper that forgets to forward the
argument produces no error, just a permanently empty ``details``.

That makes these round-trip assertions the only thing standing between a
missed passthrough and a silent loss of error detail in every caller.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
import greeter.greeter.worker as worker_grpc
import pytest
from grpclib.const import Status
from grpclib.encoding.base import StatusDetailsCodecBase
from grpclib.exceptions import GRPCError
from grpclib_transports.inproc import inproc_worker
from grpclib_transports.multiprocessing import multiprocessing_worker
from grpclib_transports.stdio import stdio_worker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DETAILS = {"kind": "structured", "frames": ["outer", "inner"]}


class JsonStatusDetailsCodec(StatusDetailsCodecBase):
    """Minimal JSON codec -- enough to prove the payload survives the trailer."""

    def encode(self, status: Status, message: str | None, details: Any) -> bytes:
        return json.dumps(details).encode("utf-8")

    def decode(self, status: Status, message: str | None, data: bytes) -> Any:
        return json.loads(data.decode("utf-8"))


class FailingGreeter(server_grpc.GreeterBase):
    """Always fails, attaching structured details to the error."""

    async def say_hello(self, message: common_pb2.HelloRequest) -> common_pb2.HelloReply:
        raise GRPCError(Status.UNKNOWN, "boom", DETAILS)

    async def upload(self, messages: AsyncIterator[common_pb2.HelloRequest]) -> common_pb2.HelloReply:
        raise GRPCError(Status.UNKNOWN, "boom", DETAILS)


class FailingWorkerGreeter(worker_grpc.GreeterWorkerBase):
    """``FailingGreeter`` on the worker service, for the subprocess transports."""

    async def say_hello(self, message: common_pb2.HelloRequest) -> common_pb2.HelloReply:
        raise GRPCError(Status.UNKNOWN, "boom", DETAILS)

    async def upload(self, messages: AsyncIterator[common_pb2.HelloRequest]) -> common_pb2.HelloReply:
        raise GRPCError(Status.UNKNOWN, "boom", DETAILS)


def failing_worker_services() -> list[FailingWorkerGreeter]:
    """Module-level so the forkserver transport can pickle it by reference."""
    return [FailingWorkerGreeter()]


# Mirrors test_stdio.py's `sys.executable -c` pattern: the stdio worker is a
# real subprocess, so its service and codec have to be defined in its own code.
_STDIO_FAILING_WORKER_CODE = """
from __future__ import annotations

import asyncio
import json

import greeter.greeter.worker as worker_grpc
from grpclib.const import Status
from grpclib.encoding.base import StatusDetailsCodecBase
from grpclib.exceptions import GRPCError
from grpclib_transports.stdio import serve_stdio

DETAILS = {"kind": "structured", "frames": ["outer", "inner"]}


class JsonStatusDetailsCodec(StatusDetailsCodecBase):
    def encode(self, status, message, details):
        return json.dumps(details).encode("utf-8")

    def decode(self, status, message, data):
        return json.loads(data.decode("utf-8"))


class FailingWorkerGreeter(worker_grpc.GreeterWorkerBase):
    async def say_hello(self, message):
        raise GRPCError(Status.UNKNOWN, "boom", DETAILS)

    async def upload(self, messages):
        raise GRPCError(Status.UNKNOWN, "boom", DETAILS)


asyncio.run(
    serve_stdio(
        [FailingWorkerGreeter()],
        status_details_codec=JsonStatusDetailsCodec(),
    )
)
"""


async def test_inproc_worker_round_trips_status_details() -> None:
    async with inproc_worker(
        lambda: [FailingGreeter()],
        status_details_codec=JsonStatusDetailsCodec(),
    ) as channel:
        stub = server_grpc.GreeterStub(channel)
        with pytest.raises(GRPCError) as excinfo:
            await stub.say_hello(common_pb2.HelloRequest(name="x"))

    assert excinfo.value.details == DETAILS


async def test_inproc_worker_without_codec_drops_details_silently() -> None:
    """The failure mode this whole module guards against.

    No exception, no warning -- ``details`` is simply gone. Pinning it makes
    the difference between "wired" and "not wired" observable.
    """
    async with inproc_worker(lambda: [FailingGreeter()]) as channel:
        stub = server_grpc.GreeterStub(channel)
        with pytest.raises(GRPCError) as excinfo:
            await stub.say_hello(common_pb2.HelloRequest(name="x"))

    assert excinfo.value.message == "boom"
    assert excinfo.value.details is None


async def test_multiprocessing_worker_round_trips_status_details() -> None:
    async with multiprocessing_worker(
        failing_worker_services,
        status_details_codec=JsonStatusDetailsCodec(),
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        with pytest.raises(GRPCError) as excinfo:
            await stub.say_hello(common_pb2.HelloRequest(name="x"))

    assert excinfo.value.details == DETAILS


async def test_stdio_worker_round_trips_status_details() -> None:
    async with stdio_worker(
        [sys.executable, "-c", _STDIO_FAILING_WORKER_CODE],
        status_details_codec=JsonStatusDetailsCodec(),
    ) as channel:
        stub = worker_grpc.GreeterWorkerStub(channel)
        with pytest.raises(GRPCError) as excinfo:
            await stub.say_hello(common_pb2.HelloRequest(name="x"))

    assert excinfo.value.details == DETAILS
