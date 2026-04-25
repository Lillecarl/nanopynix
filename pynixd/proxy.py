"""
Nix daemon protocol session handler.

Accepts a client connection, performs the handshake, decodes operations,
and dispatches them to request type handle() classmethods.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import asyncssh
import structlog

from . import wire
from .build_queue import BuildQueue
from .connection import ClientConn
from .exceptions import BackendError, OpNotImplementedError
from .operations import OP_REGISTRY
from .operations.base import (
    OpRequest,
    OpResponse,
    RequestContext,
    Resp,
    Role,
)
from .protocol import OptTrusted, get_extension_features
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
        role: Role = Role.USER,
        username: str = "unknown",
    ) -> None:
        self.r = client_r
        self.w = client_w
        self.client = ClientConn(w=self.w)
        self.local_store = local_store
        self.scheduler = scheduler
        self.version: int = wire.PROTOCOL_VERSION
        self.role: Role = role
        self.username: str = username

    @property
    def build_queue(self) -> BuildQueue | None:
        return self.scheduler.queue if self.scheduler else None

    @property
    def scheduler_trigger(self) -> Callable[[], None] | None:
        return self.scheduler.trigger if self.scheduler else None

    @property
    def stores(self) -> Mapping[str, Store]:
        return self.scheduler.stores if self.scheduler else {}

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

        # Present pynixd's supported protocol version to the client.
        # This enables feature negotiation (1.38+) even if the local store is older.
        server_version = wire.PROTOCOL_VERSION
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
            self.w.write_string_set(get_extension_features())  # our features
            await self.w.drain()

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

            req_cls = OP_REGISTRY.get(op_num)
            if self.scheduler:
                self.scheduler.record_activity()
            if req_cls is None:
                log.warning("unknown_op", op_num=op_num)
                await self.send_error(f"Unsupported operation: {op_num}")
                continue

            op_name = req_cls.name
            structlog.contextvars.bind_contextvars(operation=op_name)
            log.debug("received")

            try:
                response = await self.dispatch(op_num)

                if response is not None:
                    await self.client.flush()
                    await response.to_writer(self.w, self.version)
                    await self.w.drain()
                # else: already handled (streaming, error, etc.)

            except Exception:
                log.exception("handle_op_error", name=op_name)
                await self.client.flush()
                await self.send_error(f"Internal error handling {op_name}")

    # ── Dispatch ─────────────────────────────────────────────────────

    async def execute(
        self,
        request: OpRequest[Resp],
    ) -> Resp:
        """Execute an operation, falling back to other stores for extensions."""
        local_resp: Resp | None = None
        try:
            local_resp = await self.local_store.execute(request, client=self.client)
            if not (request.is_extension and local_resp.is_not_found):
                return local_resp
        except OpNotImplementedError:
            if not request.is_extension:
                raise

        # Extension not supported by local store or returned not found — try other stores
        for store in self.stores.values():
            try:
                # We don't forward client logs to remote stores for simple queries
                # unless they are builds.
                resp = await store.execute(request, client=self.client)
                if not resp.is_not_found:
                    return resp
            except OpNotImplementedError:
                continue

        # If we got here, none of the backends had a "found" result.
        # Return the local_store result (even if empty) as the final word,
        # unless it wasn't even implemented.
        if local_resp is not None:
            return local_resp

        raise OpNotImplementedError(
            f"Extension operation {type(request).__name__} (op={request.op}) "
            "not supported by any configured store",
        )

    async def dispatch(self, op_num: int) -> OpResponse | None:
        """Route an operation to its request type's handle method."""
        req_cls = OP_REGISTRY.get(op_num)
        if req_cls is None:
            log.warning("unhandled_op", op_num=op_num)
            await self.send_error(f"Unhandled operation: {op_num}")
            return None

        # Build request context
        ctx = RequestContext(
            proxy=self,
            role=self.role,
            version=self.version,
            username=self.username,
        )

        try:
            request = req_cls()
            return await request.handle(ctx)
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
            ),
        )
        await self.client.flush()
