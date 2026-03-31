"""
Nix daemon protocol session handler.

Accepts a client connection, performs the handshake, decodes operations,
and dispatches them to request type handle() classmethods.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import asyncssh

from . import wire
from .build_queue import BuildQueue
from .connection import ClientConn
from .exceptions import BackendError
from .operations import OP_REGISTRY
from .operations.base import (
    ByteCollector,
    OpResponse,
)
from .protocol import Op, OptTrusted, op_log
from .stderr import StderrError
from .store import Store
from .wire import NixReader, NixWriter

log: logging.Logger = logging.getLogger(__name__)

NIX_VERSION: str = "pynixd-0.1.0"


class DaemonProxy:
    """Per-client session: handshake, op dispatch, response encoding.

    Each client connection gets its own DaemonProxy. Connection instances
    are acquired from the local_store pool per-operation and returned
    immediately, keeping pool usage minimal.
    """

    def __init__(
        self,
        client_r: NixReader,
        client_w: NixWriter,
        *,
        local_store: Store,
        build_queue: BuildQueue | None = None,
        scheduler_trigger: Callable[[], None] | None = None,
    ) -> None:
        self._r = client_r
        self._w = client_w
        self._client = ClientConn(w=self._w)
        self.local_store = local_store
        self._build_queue = build_queue
        self._scheduler_trigger = scheduler_trigger
        self._version: int = wire.PROTOCOL_VERSION

    async def run(self) -> None:
        """Run the full session lifecycle."""
        self._client.start()
        try:
            await self._handshake()
            await self._op_loop()
        except (EOFError, BrokenPipeError, ConnectionError, OSError):
            log.debug("Client disconnected")
        except Exception:
            log.exception("Session error")
        finally:
            await self._client.stop()

    # ── Handshake ────────────────────────────────────────────────────

    async def _handshake(self) -> None:
        """Server-side daemon protocol handshake."""
        magic = await self._r.read_uint64()
        if magic != wire.WORKER_MAGIC_1:
            raise ValueError(f"Bad client magic: {magic:#x}")

        # Present the local store's protocol version to the client
        server_version = self.local_store.version
        self._w.write_uint64(wire.WORKER_MAGIC_2)
        self._w.write_uint64(server_version)
        await self._w.drain()

        client_version = await self._r.read_uint64()
        self._version = min(server_version, client_version)
        log.info(
            "Client protocol version: %s, local store version: %s, negotiated: %s",
            wire.proto_str(client_version),
            wire.proto_str(server_version),
            wire.proto_str(self._version),
        )

        # Feature negotiation (1.38+) — before CPU/reserveSpace
        if self._version >= wire.proto(1, 38):
            client_features = await self._r.read_string_set()
            log.debug("Client features: %s", client_features)
            self._w.write_string_set(set())  # our features (none)

        if await self._r.read_uint64():  # sendCpu
            await self._r.read_uint64()  # cpuAffinity (ignored)
        await self._r.read_uint64()  # reserveSpace (ignored)

        # Server conditions these on clientVersion
        if client_version >= wire.proto(1, 33):
            self._w.write_string(NIX_VERSION)
            if client_version >= wire.proto(1, 35):
                self._w.write_uint64(OptTrusted.Trusted)
        self._w.write_uint64(wire.STDERR_LAST)
        await self._w.drain()

        log.info("Client handshake complete")

    # ── Op loop ──────────────────────────────────────────────────────

    async def _op_loop(self) -> None:
        """Read ops, dispatch, write responses."""
        while True:
            try:
                op_num = await self._r.read_uint64()
            except (EOFError, asyncssh.misc.ConnectionLost):
                break

            try:
                op = Op(op_num)
                op_log(op.name).debug("recvOp: %s (%d)", op.name, op_num)
            except ValueError:
                log.warning("Unknown op: %d", op_num)
                await self._send_error(f"Unsupported operation: {op_num}")
                continue

            try:
                response = await self._dispatch(op)

                if response is not None:
                    # Flush any queued stderr before sending the response
                    await self._client.flush()
                    # Buffer the entire response so it becomes one SSH write
                    buf = ByteCollector()
                    buf.write_uint64(wire.STDERR_LAST)
                    await response.to_writer(buf, self._version)
                    self._w.write(buf.getvalue())
                    await self._w.drain()
                    op_log(op.name).debug("sendOp: %s - done", op.name)
                # else: already handled (streaming, error, etc.)

            except Exception:
                log.exception("Error handling op %s", op.name)
                await self._client.flush()
                await self._send_error(f"Internal error handling {op.name}")

    # ── Dispatch ─────────────────────────────────────────────────────

    async def _dispatch(self, op: Op) -> OpResponse | None:
        """Route an operation to its request type's handle method."""
        req_cls = OP_REGISTRY.get(op.value)
        if req_cls is None:
            log.warning("Unhandled op: %s (%d)", op.name, op.value)
            await self._send_error(f"Unhandled operation: {op.name}")
            return None

        try:
            return await req_cls.handle(self)
        except BackendError:
            return None

    # ── Helpers ───────────────────────────────────────────────────────

    async def _send_error(self, msg: str) -> None:
        """Send a STDERR_ERROR to the client."""
        self._client.queue.put_nowait(
            StderrError(
                error_type="Error",
                level=0,
                name="Error",
                msg=msg,
                have_pos=0,
                traces=[],
            )
        )
        await self._client.flush()
