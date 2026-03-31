"""
Nix daemon protocol session handler.

Accepts a client connection, performs the handshake, decodes operations,
and dispatches them to request type handle() classmethods.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import asyncssh

from . import wire
from .build_queue import BuildQueue
from .connection import ClientConn
from .derived_path import DerivedPath
from .drv_parser import (
    read_drv_file,
    to_basic_derivation,
)
from .exceptions import BackendError
from .operations import OP_REGISTRY
from .operations.base import (
    ByteCollector,
    OpResponse,
    Uint64Response,
)
from .operations.builds import (
    BuildDerivationRequest,
    BuildDerivationResponse,
    BuildPathsRequest,
    BuildPathsWithResultsRequest,
    KeyedBuildResult,
    KeyedBuildResultsResponse,
)
from .operations.queries import QueryMissingRequest
from .protocol import Op, OptTrusted, op_log
from .stderr import StderrError, StderrNext
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

    # ── Builds ───────────────────────────────────────────────────────

    async def _enqueue_build_derivation(
        self,
        request: BuildDerivationRequest,
    ) -> asyncio.Future[OpResponse]:
        """Enqueue a single BuildDerivation request."""
        if self._build_queue is None:
            raise RuntimeError("Build queue not configured")

        # Enrich with .drv metadata (e.g. _is_dynamic) if not already set
        self._enrich_derivation(request)

        build_id, future = await self._build_queue.enqueue(
            Op.BuildDerivation,
            request,
            self._client,
            set(request.derivation.input_srcs),
            platform=request.derivation.platform,
        )
        log.info("Build %d enqueued (BuildDerivation %s)", build_id, request.drv_path)

        if self._scheduler_trigger is not None:
            self._scheduler_trigger()

        return future

    def _enrich_derivation(self, request: BuildDerivationRequest) -> None:
        """Set _is_dynamic from the .drv file on disk.

        When BuildDerivation comes over the wire, _is_dynamic defaults to
        False. Parse the actual .drv to detect DrvWithVersion format so
        supports_lix() works correctly for store routing.
        """
        store_path = self.local_store.store_path or ""
        if not store_path:
            return
        try:
            parsed = read_drv_file(store_path, request.drv_path)
            request.derivation._is_dynamic = parsed.is_dynamic
        except FileNotFoundError:
            log.debug("Cannot enrich %s: .drv not in local store", request.drv_path)
        except Exception:
            log.debug("Cannot enrich %s", request.drv_path, exc_info=True)

    async def _decompose_build_paths(
        self,
        request: BuildPathsRequest | BuildPathsWithResultsRequest,
    ) -> list[tuple[str, set[str], asyncio.Future[OpResponse]]]:
        """Decompose BuildPaths into individual BuildDerivation requests.

        Returns list of (derived_path, output_names, future) tuples.
        """
        store_path = self.local_store.store_path or ""

        # Query which drvs actually need building
        missing_resp = await self.local_store.query_missing(
            QueryMissingRequest(derived_paths=request.derived_paths)
        )

        results: list[tuple[str, set[str], asyncio.Future[OpResponse]]] = []

        # Resolve all builds first so we can batch-discover input paths
        resolved: list[tuple[str, set[str], BuildDerivationRequest]] = []
        all_input_srcs: set[str] = set()

        for dp in (DerivedPath(p) for p in missing_resp.will_build):
            try:
                parsed = dp.to_derivation(store_path)
            except FileNotFoundError:
                log.warning("Cannot read drv %s for decomposition", dp.drv_path)
                continue

            basic = to_basic_derivation(parsed, store_path)
            drv_request = BuildDerivationRequest(
                drv_path=dp.drv_path,
                derivation=basic,
                build_mode=request.build_mode,
            )
            resolved.append((str(dp), dp.output_names, drv_request))
            all_input_srcs.update(basic.input_srcs)

        # Discover paths that exist on the local store but aren't tracked.
        # This is needed for Unix socket stores where nix writes paths
        # directly to the store filesystem, bypassing the daemon protocol
        # (so pynixd never sees AddToStore for them).
        unknown = all_input_srcs - self.local_store.known_paths
        if unknown:
            valid = await self.local_store.query_valid_paths(unknown)
            self.local_store.add_known_paths(valid, update_regtime=False)

        for dp, output_names, drv_request in resolved:
            future = await self._enqueue_build_derivation(drv_request)
            results.append((dp, output_names, future))

        return results

    async def _build_paths(self, request: BuildPathsRequest) -> OpResponse:
        op_log("BuildPaths").debug(
            "BuildPaths len(paths)=%d", len(request.derived_paths)
        )
        decomposed = await self._decompose_build_paths(request)

        if not decomposed:
            return Uint64Response(value=0)  # nothing to build

        # Await all futures
        futures = [f for _, _, f in decomposed]
        responses = await asyncio.gather(*futures)

        # Any failure → overall failure
        for resp in responses:
            if isinstance(resp, BuildDerivationResponse):
                if resp.result.status != 0:
                    return Uint64Response(value=1)

        return Uint64Response(value=0)

    async def _build_paths_with_results(
        self, request: BuildPathsWithResultsRequest
    ) -> OpResponse:
        op_log("BuildPathsWithResults").debug(
            "BuildPathsWithResults len(drvs)=%d", len(request.derived_paths)
        )
        decomposed = await self._decompose_build_paths(request)

        if not decomposed:
            return KeyedBuildResultsResponse(results=[])

        # Await all futures
        futures = [f for _, _, f in decomposed]
        responses = await asyncio.gather(*futures)

        # Compose KeyedBuildResults from individual BuildDerivationResponses
        keyed_results: list[KeyedBuildResult] = []
        for (dp, _, _), resp in zip(decomposed, responses):
            if isinstance(resp, BuildDerivationResponse):
                keyed_results.append(
                    KeyedBuildResult(
                        derived_path=dp,
                        result=resp.result,
                    )
                )
                if resp.result.status not in (0, 1, 2):
                    log.warning(
                        "Unexpected BuildPathsWithResults status=%d: %s",
                        resp.result.status,
                        resp.result.error_msg,
                    )
                if resp.result.status != 0 and resp.result.error_msg:
                    self._client.queue.put_nowait(
                        StderrNext(text=f"pynixd: {resp.result.error_msg}\n")
                    )

        return KeyedBuildResultsResponse(results=keyed_results)

    async def _build_derivation(self, request: BuildDerivationRequest) -> OpResponse:
        # Discover paths that exist on the local store but aren't tracked.
        # When clients connect via Unix domain sockets, nix writes paths
        # directly to the store filesystem, bypassing the daemon protocol
        # (so pynixd never sees AddToStore for them).
        unknown = (
            set(request.derivation.input_srcs) | {request.drv_path}
        ) - self.local_store.known_paths
        if unknown:
            valid = await self.local_store.query_valid_paths(unknown)
            self.local_store.add_known_paths(valid, update_regtime=False)

        future = await self._enqueue_build_derivation(request)
        response = await future
        if isinstance(response, BuildDerivationResponse):
            if response.result.status not in (0, 1, 2):
                log.warning(
                    "Unexpected BuildDerivation status=%d: %s",
                    response.result.status,
                    response.result.error_msg,
                )
            if response.result.status != 0 and response.result.error_msg:
                self._client.queue.put_nowait(
                    StderrNext(text=f"pynixd: {response.result.error_msg}\n")
                )
            op_log("BuildDerivation").debug(
                "BuildDerivation %s completed: status=%d, outputs=%s",
                request.drv_path,
                response.result.status,
                list(response.result.built_outputs.keys()),
            )
        return response

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
