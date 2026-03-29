"""
Nix daemon protocol client.

Connection is a concrete class that takes a pre-established reader/writer pair
and speaks the daemon protocol over it. Transport setup (subprocess, SSH,
Unix socket) is handled by Store types.

Lifecycle:
    r, w = <transport-specific setup>
    conn = Connection(r, w, "my-store")
    await conn.connect()          # handshake
    result = await conn.call(...)  # or typed methods
    await conn.close()
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from . import stderr, wire
from .exceptions import BackendError
from .operations.base import (
    ByteCollector,
    OpRequest,
    Resp,
)
from .protocol import Op, op_log
from .wire import (
    NixReader,
    NixWriter,
)

log: logging.Logger = logging.getLogger(__name__)
stderr_log: logging.Logger = logging.getLogger("pynixd.stderr")


# ── Shared types ────────────────────────────────────────────────────


class ClientConn:
    """Client connection with a stderr queue for non-blocking forwarding.

    Build tasks put stderr messages on the queue via stderr.collect().
    A drain task serializes messages to the client socket — no lock needed
    since the drain task is the sole writer of stderr data.

    Call flush() before writing the response to ensure all stderr is sent.
    """

    def __init__(self, w: NixWriter) -> None:
        self.w = w
        self.queue: asyncio.Queue[stderr.StderrMsg | None] = asyncio.Queue()
        self._drain_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background drain task."""
        self._drain_task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        """Stop the drain task."""
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

    async def flush(self) -> None:
        """Wait until all queued stderr messages have been written and flushed."""
        await self.queue.join()
        await self.w.drain()

    async def _drain_loop(self) -> None:
        """Consume stderr messages from the queue and write to client."""
        while True:
            msg = await self.queue.get()
            try:
                if msg is not None:
                    msg.to_writer(self.w)
                # Batch: grab any additional messages already queued
                while not self.queue.empty():
                    extra = self.queue.get_nowait()
                    if extra is not None:
                        extra.to_writer(self.w)
                    self.queue.task_done()
                # Flush buffered writes to the socket
                await self.w.drain()
            finally:
                self.queue.task_done()


# ── Connection ──────────────────────────────────────────────────────


class Connection:
    """Nix daemon protocol client over a pre-established transport.

    Takes a reader/writer pair and handles handshake, operation
    dispatch, stderr forwarding, and response decoding.
    """

    def __init__(
        self,
        r: NixReader,
        w: NixWriter,
        conn_id: str,
        store_path: str | None = None,
    ) -> None:
        self.id: str = conn_id
        self.store_path: str | None = store_path
        self.version: int = wire.PROTOCOL_VERSION
        self.r = r
        self.w = w
        self.connected: bool = False
        self.dirty: bool = False
        self._op_log: list[str] = []

    async def connect(self) -> None:
        """Perform daemon protocol handshake."""
        await self._handshake(self.r, self.w)
        self.connected = True

    async def close(self) -> None:
        """Close the connection and release the underlying transport."""
        self.connected = False
        try:
            await self.w.close()
        except Exception:
            pass

    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> Resp:
        """Send an operation on the established connection.

        Args:
            request: Request object with ClassVars for op and response_type
            client: If provided, queue stderr to this client's drain task
            suppress_last: If True, consume STDERR_LAST
                but don't write it to client
        """
        if not self.connected:
            raise RuntimeError(f"Connection {self.id!r} not connected")

        req_cls = type(request)
        op_code = req_cls.op
        response_type = req_cls.response_type

        op = Op(op_code)
        self._op_log.append(op.name)

        op_log(op.name).debug(
            "sendOp: store=%s op=%s(%d)",
            self.id,
            op.name,
            op.value,
        )
        try:
            buf = ByteCollector()
            buf.write_uint64(op)
            await request.to_writer(buf, self.version)
            self.w.write(buf.getvalue())
            await self.w.drain()

            if client is not None:
                err = await stderr.collect(self.r, client.queue)
                if err is not None:
                    stderr_log.error(
                        "store=%s daemon error: [%s] %s",
                        self.id,
                        err.error_type,
                        err.msg,
                    )
            else:
                await stderr.drain(self.r, raise_on_error=False, conn_id=self.id)

            response = await response_type.from_reader(self.r, self.version)
        except Exception:
            self.dirty = True
            raise
        op_log(op.name).debug(
            "recvOp: store=%s op=%s done",
            self.id,
            op.name,
        )
        # response_type is ClassVar[type[OpResponse]] so from_reader
        # returns OpResponse, not Resp. The actual type is correct at
        # runtime — ClassVar can't reference a class type parameter.
        return cast(Resp, response)

    # ── Handshake ───────────────────────────────────────────────────

    async def _handshake(
        self,
        r: NixReader,
        w: NixWriter,
    ) -> None:
        """Perform daemon protocol handshake (client side)."""
        self.w.write_uint64(wire.WORKER_MAGIC_1)
        await w.drain()

        magic = await self.r.read_uint64()
        if magic != wire.WORKER_MAGIC_2:
            raise ValueError(f"Bad magic: {magic:#x}")

        server_version = await self.r.read_uint64()
        self.version = min(wire.PROTOCOL_VERSION, server_version)
        log.debug(
            "Daemon protocol: %s (negotiated %s)",
            wire.proto_str(server_version),
            wire.proto_str(self.version),
        )

        self.w.write_uint64(wire.PROTOCOL_VERSION)

        # Feature negotiation (1.38+) — before CPU/reserveSpace
        if self.version >= wire.proto(1, 38):
            self.w.write_string_set(set())  # our features (none)
            await w.drain()
            server_features = await self.r.read_string_set()
            log.debug("Daemon features: %s", server_features)

        self.w.write_uint64(0)  # sendCpu
        self.w.write_uint64(0)  # reserveSpace
        await w.drain()

        # Server conditions these on clientVersion, not negotiated version
        if server_version >= wire.proto(1, 33):
            nix_version = await self.r.read_string()
            log.debug("Daemon nix version: %s", nix_version)

            if server_version >= wire.proto(1, 35):
                await self.r.read_uint64()

        # Drain initial STDERR_LAST
        await stderr.drain(self.r)

    def __repr__(self) -> str:
        return f"Connection(id={self.id!r})"
