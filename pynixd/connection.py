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
from pathlib import Path
from types import TracebackType
from typing import cast

import structlog

from . import stderr, wire
from .exceptions import InfrastructureError
from .operations.base import (
    ByteCollector,
    OpRequest,
    Resp,
)
from .protocol import get_extension_features
from .wire import (
    NixReader,
    NixWriter,
)

log = structlog.get_logger(__name__)
stderr_log = structlog.get_logger("pynixd.stderr")


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
        self.drain_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background drain task."""
        self.drain_task = asyncio.create_task(self.drain_loop())

    async def stop(self) -> None:
        """Stop the drain task."""
        if self.drain_task is not None:
            self.drain_task.cancel()
            try:
                await self.drain_task
            except asyncio.CancelledError:
                pass
            self.drain_task = None

    async def flush(self) -> None:
        """Wait until all queued stderr messages have been written and flushed."""
        await self.queue.join()
        await self.w.drain()

    async def drain_loop(self) -> None:
        """Consume stderr messages from the queue and write to client."""

        while True:
            msg = await self.queue.get()
            try:
                # Use a buffer to batch multiple messages into a single write() call.
                # This avoids O(N^2) performance issues in asyncio transport's
                # get_write_buffer_size() when the buffer contains many small pieces.
                buf = ByteCollector()
                if msg is not None:
                    msg.to_writer(buf)

                # Batch: grab any additional messages already queued
                while not self.queue.empty():
                    extra = self.queue.get_nowait()
                    if extra is not None:
                        extra.to_writer(buf)
                    self.queue.task_done()

                data = buf.getvalue()
                if data:
                    self.w.write(data)

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
        store_path: Path | None = None,
    ) -> None:
        self.id: str = conn_id
        self.store_path: Path | None = store_path
        self.version: int = wire.PROTOCOL_VERSION
        self.nix_version: str = ""
        self.features: set[str] = set()
        self.r = r
        self.w = w
        self.connected: bool = False
        self.dirty: bool = False
        self.op_log: list[str] = []

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        """Exit connection context. Marks as dirty if an exception occurred
        or if buffers are not empty.
        """
        if exc_type is not None:
            self.dirty = True

        if not self.dirty:
            if await self.r.is_dirty() or await self.w.is_dirty():
                self.dirty = True

    async def connect(self) -> None:
        """Perform daemon protocol handshake."""
        await self.handshake(self.r, self.w)
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
        raise_on_error: bool = False,
    ) -> Resp:
        """Send an operation on the established connection.

        Args:
            request: Request object with ClassVars for op and response_type
            client: If provided, queue stderr to this client's drain task
            suppress_last: If True, consume STDERR_LAST
                but don't write it to client
            raise_on_error: If True, raise BackendError on stderr errors
        """
        if not self.connected:
            raise RuntimeError(f"Connection {self.id!r} not connected")

        req_cls = type(request)
        response_type = req_cls.response_type
        op_name = req_cls.name

        self.op_log.append(op_name)

        await request.to_writer(self.w, self.version)
        await self.w.drain()

        # If client is provided, we stream logs directly to them and don't buffer
        # locally to save memory on large builds.
        response = await response_type().from_reader(
            self.r,
            self.version,
            client=client,
            buffer_logs=(client is None),
        )

        return cast(Resp, response)

    # ── Handshake ───────────────────────────────────────────────────

    async def handshake(
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
        if self.version < wire.MINIMUM_REMOTE_PROTOCOL:
            msg = (
                f"Store {self.id} negotiated protocol {wire.proto_str(self.version)}, "
                f"but we require >= {wire.proto_str(wire.MINIMUM_REMOTE_PROTOCOL)}"
            )
            log.error("protocol_version_too_low", store_id=self.id, error=msg)
            raise InfrastructureError(msg)
        log.debug(
            "daemon_protocol_negotiated",
            server_version=wire.proto_str(server_version),
            negotiated=wire.proto_str(self.version),
        )

        self.w.write_uint64(wire.PROTOCOL_VERSION)

        # Feature negotiation (1.38+) — before CPU/reserveSpace
        if self.version >= wire.proto(1, 38):
            self.w.write_string_set(get_extension_features())  # our features
            await w.drain()
            self.features = await self.r.read_string_set()
            log.debug("daemon_features", server_features=self.features)
        self.w.write_uint64(0)  # sendCpu
        self.w.write_uint64(0)  # reserveSpace
        await w.drain()

        # Server conditions these on clientVersion, not negotiated version
        if server_version >= wire.proto(1, 33):
            self.nix_version = await self.r.read_string()
            log.debug("daemon_nix_version", nix_version=self.nix_version)
            if server_version >= wire.proto(1, 35):
                await self.r.read_uint64()

        # Drain initial STDERR_LAST
        await self.r.drain_stderr()

    def __repr__(self) -> str:
        return f"Connection(id={self.id!r})"
