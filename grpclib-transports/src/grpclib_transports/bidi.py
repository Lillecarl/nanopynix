"""Bidirectional logical RPC peer over a frame transport.

Provides a request/response/event/cancel protocol independent of the
underlying byte-stream transport.  Useful for in-band control channels
between processes that already share a pipe or SSH session.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

FrameKind = Literal["request", "response", "event", "cancel"]
RequestHandler = Callable[[str, Any], Awaitable[Any]]
FrameSender = Callable[["LogicalFrame"], Awaitable[None]]
FrameReceiver = Callable[[], Awaitable["LogicalFrame | None"]]


@dataclass(frozen=True)
class LogicalFrame:
    """A logical RPC frame exchanged between peers.

    Fields:
        id: Monotonic request identifier (0 for events).
        kind: Frame kind: ``"request"``, ``"response"``, ``"event"``, or ``"cancel"``.
        method: gRPC-style method name for requests/events.
        payload: Arbitrary data carried in the frame.
        error: Error message carried in a response frame.
    """

    id: int
    kind: FrameKind
    method: str | None = None
    payload: Any = None
    error: str | None = None


class RemoteCallError(Exception):
    """Raised when a remote peer responds with an error."""


class PeerClosedError(Exception):
    """Raised when an operation is attempted on a closed peer."""


class LogicalRpcPeer:
    """Bidirectional RPC peer over a logical frame transport.

    Supports request/response (:meth:`call`), one-way events (:meth:`event`),
    and cancellation.  Spawns a background reader task via :meth:`start`.
    Close with :meth:`aclose`.

    Args:
        send_frame: Callable that sends a :class:`LogicalFrame`.
        receive_frame: Callable that returns the next :class:`LogicalFrame` or ``None``.
        handler: Optional request/event handler ``(method, payload) -> result``.
    """

    def __init__(
        self,
        *,
        send_frame: FrameSender,
        receive_frame: FrameReceiver,
        handler: RequestHandler | None = None,
    ) -> None:
        self._send_frame = send_frame
        self._receive_frame = receive_frame
        self._handler = handler
        self._next_id = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._reader_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(
            self._receive_loop(),
            name="logical-rpc-peer",
        )
        self._tasks.add(self._reader_task)
        self._reader_task.add_done_callback(self._tasks.discard)

    async def call(
        self,
        method: str,
        payload: Any = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- the deadline belongs to the RPC, not to the caller's scope: on expiry this sends a `cancel` frame to the peer, which an `anyio.fail_after` around the call cannot do -- that would abandon the local future and leave the remote handler running.
    ) -> Any:
        if self._closed:
            raise PeerClosedError("peer is closed")
        self.start()
        request_id = next(self._next_id)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        await self._send_frame(
            LogicalFrame(
                id=request_id,
                kind="request",
                method=method,
                payload=payload,
            )
        )
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            await self.cancel(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def event(self, method: str, payload: Any = None) -> None:
        if self._closed:
            raise PeerClosedError("peer is closed")
        self.start()
        await self._send_frame(
            LogicalFrame(
                id=0,
                kind="event",
                method=method,
                payload=payload,
            )
        )

    async def cancel(self, request_id: int) -> None:
        if self._closed:
            return
        await self._send_frame(LogicalFrame(id=request_id, kind="cancel"))

    async def aclose(self) -> None:
        # No `if self._closed: return` guard. `_receive_loop` sets that flag in
        # its own `finally`, so a peer that went away first -- a killed
        # process, a broken pipe -- already looks closed by the time the owner
        # calls aclose, and the guard turned the one call that reaps the
        # reader into a no-op. The body below is idempotent instead: a second
        # call finds no reader, no tasks and no pending futures.
        self._closed = True
        reader, self._reader_task = self._reader_task, None
        # The reader is in `_tasks` only while it runs: `start` adds a done
        # callback that discards it. So a reader that ended on its own is not
        # in the set, and the exception it carries surfaces later as "Task
        # exception was never retrieved", in whatever code happens to run when
        # the garbage collector notices. Take it from the attribute, which
        # outlives the set.
        tasks: list[asyncio.Task[None]] = list(self._tasks)
        if reader is not None and reader not in tasks:
            tasks.append(reader)
        for task in tasks:
            task.cancel()
        for task in tasks:
            # Both classes are the same fact here: the peer is going away, and
            # aclose is the reason. CancelledError is what a reader that was
            # still running answers; any other exception is why it had already
            # stopped. Nobody is left to act on either, and _fail_pending
            # below is what tells the callers that were waiting.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._fail_pending(PeerClosedError("peer is closed"))

    async def _receive_loop(self) -> None:
        try:
            while not self._closed:
                frame = await self._receive_frame()
                if frame is None:
                    break
                await self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            self._fail_pending(e)
            raise
        finally:
            self._closed = True
            self._fail_pending(PeerClosedError("peer is closed"))

    async def _handle_frame(self, frame: LogicalFrame) -> None:
        if frame.kind == "request":
            self._spawn_request_handler(frame)
        elif frame.kind == "response":
            self._complete_response(frame)
        elif frame.kind == "cancel":
            self._cancel_pending(frame.id)
        elif frame.kind == "event":
            await self._handle_event(frame)
        else:
            raise ValueError(f"unknown logical frame kind: {frame.kind!r}")

    def _spawn_request_handler(self, frame: LogicalFrame) -> None:
        task = asyncio.create_task(
            self._handle_request(frame),
            name=f"logical-rpc-request-{frame.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_request(self, frame: LogicalFrame) -> None:
        if frame.method is None:
            await self._send_error(frame.id, "request frame is missing method")
            return
        if self._handler is None:
            await self._send_error(frame.id, f"no handler for {frame.method!r}")
            return
        try:
            result = await self._handler(frame.method, frame.payload)
        except Exception as e:
            await self._send_error(frame.id, str(e))
        else:
            await self._send_frame(
                LogicalFrame(
                    id=frame.id,
                    kind="response",
                    payload=result,
                )
            )

    async def _handle_event(self, frame: LogicalFrame) -> None:
        if self._handler is not None and frame.method is not None:
            await self._handler(frame.method, frame.payload)

    def _complete_response(self, frame: LogicalFrame) -> None:
        future = self._pending.get(frame.id)
        if future is None or future.done():
            return
        if frame.error is not None:
            future.set_exception(RemoteCallError(frame.error))
        else:
            future.set_result(frame.payload)

    def _cancel_pending(self, request_id: int) -> None:
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.cancel()

    async def _send_error(self, request_id: int, message: str) -> None:
        await self._send_frame(
            LogicalFrame(
                id=request_id,
                kind="response",
                error=message,
            )
        )

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
