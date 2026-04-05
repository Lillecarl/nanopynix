"""
Nix daemon protocol session handler.

Accepts a client connection, performs the handshake, decodes operations,
and dispatches them to request type handle() classmethods.
"""

from __future__ import annotations

from collections.abc import Callable

import asyncssh
import structlog

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
from .scheduler import Scheduler
from .stderr import StderrError
from .store import Store
from .wire import NixReader, NixWriter

log = structlog.get_logger(__name__)

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
        local_store: Store,
        scheduler: Scheduler | None = None,
    ) -> None:
        self.r = client_r
        self.w = client_w
        self.client = ClientConn(w=self.w)
        self.local_store = local_store
        self.scheduler = scheduler
        self.version: int = wire.PROTOCOL_VERSION

    @property
    def build_queue(self) -> BuildQueue | None:
        return self.scheduler.queue if self.scheduler else None

    @property
    def scheduler_trigger(self) -> Callable[[], None] | None:
        return self.scheduler.trigger if self.scheduler else None

    async def run(self) -> None:
        """Run the full session lifecycle."""
        self.client.start()
        try:
            await self.handshake()
            await self.op_loop()
        except (EOFError, BrokenPipeError, ConnectionError, OSError):
            log.debug("client_disconnected")
        except Exception:
            log.exception("session_error")
        finally:
            await self.client.stop()

    # ── Handshake ────────────────────────────────────────────────────

    async def handshake(self) -> None:
        """Server-side daemon protocol handshake."""
        magic = await self.r.read_uint64()
        if magic != wire.WORKER_MAGIC_1:
            raise ValueError(f"Bad client magic: {magic:#x}")

        # Present the local store's protocol version to the client
        server_version = self.local_store.version
        self.w.write_uint64(wire.WORKER_MAGIC_2)
        self.w.write_uint64(server_version)
        await self.w.drain()

        client_version = await self.r.read_uint64()
        self.version = min(server_version, client_version)
        log.info(
            "client_protocol_negotiated",
            client_version=wire.proto_str(client_version),
            local_store_version=wire.proto_str(server_version),
            negotiated_version=wire.proto_str(self.version),
        )

        # Feature negotiation (1.38+) — before CPU/reserveSpace
        if self.version >= wire.proto(1, 38):
            client_features = await self.r.read_string_set()
            log.debug("client_features", client_features=client_features)
            self.w.write_string_set(set())  # our features (none)

        if await self.r.read_uint64():  # sendCpu
            await self.r.read_uint64()  # cpuAffinity (ignored)
        await self.r.read_uint64()  # reserveSpace (ignored)

        # Server conditions these on clientVersion
        if client_version >= wire.proto(1, 33):
            self.w.write_string(NIX_VERSION)
            if client_version >= wire.proto(1, 35):
                self.w.write_uint64(OptTrusted.Trusted)
        self.w.write_uint64(wire.STDERR_LAST)
        await self.w.drain()

        log.info("client_handshake_complete")

    # ── Op loop ──────────────────────────────────────────────────────

    async def op_loop(self) -> None:
        """Read ops, dispatch, write responses."""
        while True:
            try:
                op_num = await self.r.read_uint64()
            except (EOFError, asyncssh.misc.ConnectionLost):
                break

            try:
                op = Op(op_num)
                op_log(op.name).debug("recvOp", op=op.name, op_num=op_num)
            except ValueError:
                log.warning("unknown_op", op_num=op_num)
                await self.send_error(f"Unsupported operation: {op_num}")
                continue

            try:
                response = await self.dispatch(op)

                if response is not None:
                    # Flush any queued stderr before sending the response
                    await self.client.flush()
                    # Buffer the entire response so it becomes one SSH write
                    buf = ByteCollector()
                    buf.write_uint64(wire.STDERR_LAST)
                    await response.to_writer(buf, self.version)
                    self.w.write(buf.getvalue())
                    await self.w.drain()
                    op_log(op.name).debug("sendOp", op=op.name)
                # else: already handled (streaming, error, etc.)

            except Exception:
                log.exception("handle_op_error", name=op.name)
                await self.client.flush()
                await self.send_error(f"Internal error handling {op.name}")

    # ── Dispatch ─────────────────────────────────────────────────────

    async def dispatch(self, op: Op) -> OpResponse | None:
        """Route an operation to its request type's handle method."""
        req_cls = OP_REGISTRY.get(op.value)
        if req_cls is None:
            log.warning("unhandled_op", op=op.name, op_value=op.value)
            await self.send_error(f"Unhandled operation: {op.name}")
            return None

        try:
            return await req_cls.handle(self)
        except BackendError:
            return None

    # ── Helpers ───────────────────────────────────────────────────────

    async def send_error(self, msg: str) -> None:
        """Send a STDERR_ERROR to the client."""
        self.client.queue.put_nowait(
            StderrError(
                error_type="Error",
                level=0,
                name="Error",
                msg=msg,
                have_pos=0,
                traces=[],
            )
        )
        await self.client.flush()
