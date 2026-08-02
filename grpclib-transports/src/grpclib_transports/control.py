"""In-band worker control plane over one existing gRPC connection."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import betterproto2
import grpclib
import grpclib.server
from betterproto2 import grpclib as betterproto2_grpclib
from grpclib.const import Cardinality, Handler, Status

from grpclib_transports.bidi import FrameKind, LogicalFrame, LogicalRpcPeer
from grpclib_transports.protocol import build_mapping

if TYPE_CHECKING:
    from betterproto2.grpclib.grpclib_client import MetadataLike
    from grpclib._typing import IServable
    from grpclib.metadata import Deadline

_CONTROL_ROUTE = "/grpclib_transports.control.ControlPlane/Connect"


@dataclass(eq=False, repr=False)
class ControlFrame(betterproto2.Message):
    id: int = betterproto2.field(1, betterproto2.TYPE_UINT64)
    kind: str = betterproto2.field(2, betterproto2.TYPE_STRING)
    method: str = betterproto2.field(3, betterproto2.TYPE_STRING)
    payload: bytes = betterproto2.field(4, betterproto2.TYPE_BYTES)
    error: str = betterproto2.field(5, betterproto2.TYPE_STRING)


def _logical_to_control(frame: LogicalFrame) -> ControlFrame:
    payload = frame.payload
    if payload is None:
        payload = b""
    if not isinstance(payload, bytes):
        raise TypeError("control frame payloads must be bytes")
    return ControlFrame(
        id=frame.id,
        kind=frame.kind,
        method=frame.method or "",
        payload=payload,
        error=frame.error or "",
    )


def _control_to_logical(frame: ControlFrame) -> LogicalFrame:
    if frame.kind not in {"request", "response", "event", "cancel"}:
        raise ValueError(f"unknown control frame kind: {frame.kind!r}")
    return LogicalFrame(
        id=frame.id,
        kind=cast("FrameKind", frame.kind),
        method=frame.method or None,
        payload=frame.payload,
        error=frame.error or None,
    )


class ControlPlaneStub(betterproto2_grpclib.ServiceStub):
    async def connect(
        self,
        frames: AsyncIterable[ControlFrame],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- grpclib stub API, passes through to _stream_stream
        deadline: Deadline | None = None,
        metadata: MetadataLike | None = None,
    ) -> AsyncIterator[ControlFrame]:
        async for frame in self._stream_stream(
            _CONTROL_ROUTE,
            frames,
            ControlFrame,
            ControlFrame,
            timeout=timeout,
            deadline=deadline,
            metadata=metadata,
        ):
            yield frame


class ControlPlaneBase(betterproto2_grpclib.ServiceBase):
    def connect(self, frames: AsyncIterator[ControlFrame]) -> AsyncIterator[ControlFrame]:
        raise grpclib.GRPCError(Status.UNIMPLEMENTED)

    async def __rpc_connect(
        self,
        stream: grpclib.server.Stream[ControlFrame, ControlFrame],
    ) -> None:
        responses = self.connect(stream.__aiter__())
        async for response in responses:
            await stream.send_message(response)

    def __mapping__(self) -> dict[str, Handler]:
        return {
            _CONTROL_ROUTE: Handler(
                self.__rpc_connect,
                Cardinality.STREAM_STREAM,
                ControlFrame,
                ControlFrame,
            )
        }


class _UnaryServerStream:
    def __init__(self, request: Any) -> None:
        self._request = request
        self.response: Any = None

    async def recv_message(self) -> Any:
        return self._request

    async def send_message(self, response: Any) -> None:
        self.response = response


class GrpcServiceDispatcher:
    """Dispatch control-plane requests into ordinary unary gRPC services."""

    def __init__(self, services: Collection[IServable]) -> None:
        self._mapping = build_mapping(tuple(services))

    async def handle(self, method: str, payload: Any) -> bytes:
        if not isinstance(payload, bytes):
            raise TypeError("control payload must be bytes")
        handler = cast("Any", self._mapping.get(method))
        if handler is None:
            raise LookupError(f"unknown parent service method {method!r}")
        if handler.cardinality is not Cardinality.UNARY_UNARY:
            raise TypeError(f"parent service method {method!r} is not unary-unary")

        request = handler.request_type().parse(payload)
        stream = _UnaryServerStream(request)
        await handler.func(stream)
        if stream.response is None:
            raise RuntimeError(f"parent service method {method!r} did not send a response")
        return bytes(stream.response)


class WorkerBackchannel:
    """Worker-side handle for calling services hosted by the parent."""

    def __init__(self) -> None:
        self._peer_ready: asyncio.Future[LogicalRpcPeer] | None = None

    def service(self) -> ControlPlaneBase:
        backchannel = self

        class _Service(ControlPlaneBase):
            async def connect(self, frames: AsyncIterator[ControlFrame]) -> AsyncIterator[ControlFrame]:
                async with _control_peer_from_server_stream(frames) as (peer, outgoing):
                    backchannel._set_peer(peer)
                    try:
                        while True:
                            frame = await outgoing.get()
                            if frame is None:
                                break
                            yield _logical_to_control(frame)
                    finally:
                        backchannel._clear_peer()

        return _Service()

    async def call_unary(self, method: str, request: Any, response_type: type[Any]) -> Any:
        peer = await self._get_peer()
        payload = await peer.call(method, bytes(request))
        if not isinstance(payload, bytes):
            raise TypeError("control response payload must be bytes")
        return response_type().parse(payload)

    async def _get_peer(self) -> LogicalRpcPeer:
        ready = self._peer_ready
        if ready is None:
            loop = asyncio.get_running_loop()
            ready = loop.create_future()
            self._peer_ready = ready
        return await ready

    def _set_peer(self, peer: LogicalRpcPeer) -> None:
        ready = self._peer_ready
        if ready is None:
            ready = asyncio.get_running_loop().create_future()
            self._peer_ready = ready
        if not ready.done():
            ready.set_result(peer)

    def _clear_peer(self) -> None:
        self._peer_ready = None


@contextlib.asynccontextmanager
async def open_parent_control_peer(
    channel: Any,
    parent_services: Collection[IServable],
) -> AsyncGenerator[LogicalRpcPeer]:
    dispatcher = GrpcServiceDispatcher(parent_services)
    async with _open_control_peer(channel, handler=dispatcher.handle) as peer:
        yield peer


@contextlib.asynccontextmanager
async def _open_control_peer(
    channel: Any,
    *,
    handler: Any = None,
) -> AsyncGenerator[LogicalRpcPeer]:
    outgoing: asyncio.Queue[LogicalFrame | None] = asyncio.Queue()

    async def send_frame(frame: LogicalFrame) -> None:
        await outgoing.put(frame)

    async def frame_source() -> AsyncIterator[ControlFrame]:
        while True:
            frame = await outgoing.get()
            if frame is None:
                break
            yield _logical_to_control(frame)

    stub = ControlPlaneStub(channel)
    responses = cast("AsyncGenerator[ControlFrame]", stub.connect(frame_source()))

    async def receive_frame() -> LogicalFrame | None:
        try:
            return _control_to_logical(await anext(responses))
        except StopAsyncIteration:
            return None

    peer = LogicalRpcPeer(send_frame=send_frame, receive_frame=receive_frame, handler=handler)
    peer.start()
    try:
        yield peer
    finally:
        await outgoing.put(None)
        await peer.aclose()
        await responses.aclose()


@contextlib.asynccontextmanager
async def _control_peer_from_server_stream(
    frames: AsyncIterator[ControlFrame],
) -> AsyncGenerator[tuple[LogicalRpcPeer, asyncio.Queue[LogicalFrame | None]]]:
    outgoing: asyncio.Queue[LogicalFrame | None] = asyncio.Queue()

    async def send_frame(frame: LogicalFrame) -> None:
        await outgoing.put(frame)

    async def receive_frame() -> LogicalFrame | None:
        try:
            return _control_to_logical(await anext(frames))
        except StopAsyncIteration:
            return None

    peer = LogicalRpcPeer(send_frame=send_frame, receive_frame=receive_frame)
    peer.start()
    try:
        yield peer, outgoing
    finally:
        await outgoing.put(None)
        await peer.aclose()
