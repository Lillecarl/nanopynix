"""
Nix daemon protocol session handler.

Accepts a client connection, performs the handshake, decodes operations,
routes them, and encodes responses.

Routing rules:
- Queries: answered from the local store (per-op pool connection)
- Mutations (AddToStore*): streamed to the local store (per-op pool connection)
- Builds: enqueued to the build queue, scheduler picks a backend
- Maintenance (SetOptions, GC): handled locally
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import asyncssh

from . import wire
from .build_queue import BuildQueue
from .connection import ClientConn, Connection
from .drv_parser import (
    parse_derived_paths,
    read_drv_file,
    to_basic_derivation,
)
from .exceptions import BackendError
from .operations import OP_REGISTRY
from .operations.base import (
    ByteCollector,
    EmptyResponse,
    OpRequest,
    OpResponse,
    SingleStringRequest,
    StringSetRequest,
    StringSetResponse,
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
from .operations.profiling import (
    StartProfilingResponse,
    StopProfilingResponse,
)
from .operations.queries import (
    NarFromPathResponse,
    QueryAllValidPathsRequest,
    QueryMissingRequest,
    QueryMissingResponse,
    QueryValidPathsRequest,
)
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
        """Route an operation. Returns None if already handled."""
        # Streaming mutations — delegated to Store
        match op:
            case Op.AddToStoreNar:
                path = await self.local_store.add_to_store_nar_streaming(self._r)
                self.local_store.add_known_path(path)
                return EmptyResponse()
            case Op.AddToStore:
                resp = await self.local_store.add_to_store_streaming(self._r)
                self.local_store.add_known_path(resp.info.path)
                return resp
            case Op.AddMultipleToStore:
                paths = await self.local_store.add_multiple_to_store_streaming(self._r)
                self.local_store.add_known_paths(set(paths))
                return EmptyResponse()

        # NarFromPath — handled via Store methods, no conn needed
        if op == Op.NarFromPath:
            req_cls = OP_REGISTRY.get(op.value)
            assert req_cls is not None
            request = await req_cls.from_reader(self._r, self._version)
            assert isinstance(request, SingleStringRequest)
            if await self.local_store.is_valid_path(request.path):
                op_log("NarFromPath").debug(
                    "NarFromPath %s -> streaming to client",
                    request.path,
                )
                nar_size = 0
                path_info = await self.local_store.query_path_info(request.path)
                if path_info is not None:
                    nar_size = path_info.nar_size
                else:
                    raise BackendError("getting status of %s", request.path)
                await self._client.flush()
                self._w.write_uint64(wire.STDERR_LAST)
                await self.local_store.stream_nar_from_path(
                    path=request.path,
                    dst=self._w,
                    nar_size=nar_size,
                )
                await self._w.drain()
                return None
            log.warning("NarFromPath %s not in local store", request.path)
            return NarFromPathResponse(nar_data=b"")

        # Decode request
        req_cls = OP_REGISTRY.get(op.value)
        if req_cls is None:
            log.warning("Unhandled op: %s (%d)", op.name, op.value)
            await self._send_error(f"Unhandled operation: {op.name}")
            return None

        request = await req_cls.from_reader(self._r, self._version)

        try:
            return await self._handle(op, request)
        except BackendError:
            return None

    async def _handle(
        self,
        op: Op,
        request: OpRequest,
    ) -> OpResponse | None:
        """Handle a decoded operation.

        Returns None if the response was already sent (streaming, etc.).
        """
        # Build ops don't need a local store — scheduler manages its own
        match op:
            case Op.PynixdStartProfiling:
                return StartProfilingResponse()
            case Op.PynixdStopProfiling:
                return StopProfilingResponse()
            case Op.BuildPaths:
                assert isinstance(request, BuildPathsRequest)
                return await self._build_paths(request)
            case Op.BuildPathsWithResults:
                assert isinstance(request, BuildPathsWithResultsRequest)
                return await self._build_paths_with_results(request)
            case Op.BuildDerivation:
                assert isinstance(request, BuildDerivationRequest)
                return await self._build_derivation(request)

        # Try fast-path SQLite reads before acquiring a daemon connection
        db = self.local_store.db
        if db is not None:
            match op:
                case Op.IsValidPath:
                    assert isinstance(request, SingleStringRequest)
                    result = await db.is_valid_path(request.path)
                    if result is not None:
                        db.mark_path(request.path)
                        return result
                case Op.QueryPathInfo:
                    assert isinstance(request, SingleStringRequest)
                    result = await db.query_path_info(request.path)
                    if result is not None:
                        db.mark_path(request.path)
                        return result
                case Op.QueryValidPaths:
                    assert isinstance(request, QueryValidPathsRequest)
                    result = await db.query_valid_paths(request.paths)
                    if result is not None:
                        db.mark_paths(result.paths)
                        return result
                case Op.QueryAllValidPaths:
                    # No regtime update — bulk query would mark everything as recent
                    result = await db.query_all_valid_paths()
                    if result is not None:
                        return result

        # Everything else acquires a conn for the duration of the op
        async with self.local_store.transfer_conn() as conn:
            match op:
                # ── Queries (local store) — fallback if DB unavailable
                case Op.IsValidPath | Op.QueryPathInfo | Op.QueryValidPaths:
                    return await conn.call(
                        request, client=self._client, suppress_last=True
                    )

                case Op.QueryAllValidPaths:
                    return await self._query_all_valid_paths(conn)

                case Op.QueryMissing:
                    return await self._query_missing(request, conn)

                # ── Maintenance ────────────────────────────────────
                case Op.SetOptions:
                    return EmptyResponse()

                case (
                    Op.OptimiseStore
                    | Op.VerifyStore
                    | Op.AddTempRoot
                    | Op.AddIndirectRoot
                    | Op.CollectGarbage
                    | Op.AddSignatures
                    | Op.AddPermRoot
                ):
                    return await self._unimplemented(request)

                # ── Fallback: forward to local store ──────────────
                case _:
                    return await conn.call(
                        request, client=self._client, suppress_last=True
                    )

    async def _query_all_valid_paths(self, conn: Connection) -> OpResponse:
        all_paths: set[str] = set()
        try:
            resp = await conn.call(
                QueryAllValidPathsRequest(),
                client=self._client,
                suppress_last=True,
            )
            all_paths.update(resp.paths)
        except Exception:
            log.debug("Local QueryAllValidPaths failed")
        return StringSetResponse(paths=all_paths)

    async def _query_missing(self, request: OpRequest, conn: Connection) -> OpResponse:
        assert isinstance(request, StringSetRequest)

        op_log("QueryMissing").debug("QueryMissing len(targets)=%d", len(request.paths))

        drv_paths = {dp.split("!")[0] if "!" in dp else dp for dp in request.paths}

        try:
            resp = await conn.call(
                QueryValidPathsRequest(paths=drv_paths, substitute=0),
                client=self._client,
                suppress_last=True,
            )
            known_paths = resp.paths
        except Exception:
            log.exception("Local query_valid_paths failed")
            known_paths = set()

        will_build = {
            dp
            for dp in request.paths
            if (dp.split("!")[0] if "!" in dp else dp) not in known_paths
        }
        return QueryMissingResponse(
            will_build=will_build,
            will_substitute=set(),
            unknown=set(),
            download_size=0,
            nar_size=0,
        )

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

        # Compute full runtime closure at enqueue time so ranking/transfer
        # never need to recompute it.
        required_paths = await self._compute_closure(set(request.derivation.input_srcs))

        build_id, future = await self._build_queue.enqueue(
            Op.BuildDerivation,
            request,
            self._client,
            required_paths,
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
        drv_map = parse_derived_paths(request.derived_paths)

        # Query which drvs actually need building
        async with self.local_store.transfer_conn() as conn:
            missing_resp = await conn.call(
                QueryMissingRequest(paths=request.derived_paths)
            )

            # Substitute missing paths to local store
            if missing_resp.will_substitute:
                log.info(
                    "Substituting %d paths to local store",
                    len(missing_resp.will_substitute),
                )
                valid_resp = await conn.call(
                    QueryValidPathsRequest(
                        paths=missing_resp.will_substitute,
                        substitute=1,
                    )
                )
                valid = valid_resp.paths
                self.local_store.add_known_paths(valid)

        # Collect the set of drv paths we'll be building so
        # to_basic_derivation can recurse through them for transitive
        # output paths without walking the entire stdenv graph.
        building_drvs = {
            dp.split("!")[0] if "!" in dp else dp
            for dp in missing_resp.will_build
        }

        results: list[tuple[str, set[str], asyncio.Future[OpResponse]]] = []

        for dp in missing_resp.will_build:
            drv_path = dp.split("!")[0] if "!" in dp else dp
            output_names = drv_map.get(drv_path, {"*"})

            try:
                parsed = read_drv_file(store_path, drv_path)
            except FileNotFoundError:
                log.warning("Cannot read drv %s for decomposition", drv_path)
                continue

            basic = to_basic_derivation(parsed, store_path, building_drvs)
            drv_request = BuildDerivationRequest(
                drv_path=drv_path,
                derivation=basic,
                build_mode=request.build_mode,
            )

            future = await self._enqueue_build_derivation(drv_request)
            results.append((dp, output_names, future))

        return results

    async def _build_paths(self, request: BuildPathsRequest) -> OpResponse:
        op_log("BuildPaths").debug("BuildPaths len(paths)=%d", len(request.derived_paths))
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
        op_log("BuildPathsWithResults").debug("BuildPathsWithResults len(drvs)=%d", len(request.derived_paths))
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

    async def _compute_closure(self, seeds: set[str]) -> set[str]:
        """Expand seeds to full runtime reference closure.

        Fast path: single SQLite recursive CTE.
        Slow path: sequential query_path_info walks.
        """
        db = self.local_store.db
        if db is not None:
            closure = await db.compute_closure(seeds)
            if closure is not None:
                # The CTE only returns paths in ValidPaths — seeds for
                # not-yet-built outputs would be silently dropped.  Always
                # preserve the original seeds so _is_schedulable blocks
                # until those outputs actually exist in the local store.
                return closure | seeds

        # Slow path: walk references via daemon protocol
        closure = set(seeds)
        queue = list(seeds)
        while queue:
            path = queue.pop()
            try:
                path_info = await self.local_store.query_path_info(path)
                if path_info:
                    for ref in path_info.references:
                        if ref not in closure:
                            closure.add(ref)
                            queue.append(ref)
            except Exception:
                pass
        return closure

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

    async def _unimplemented(
        self,
        request: OpRequest,
    ) -> OpResponse:
        """Warn the client and return a default response for unimplemented ops."""
        req_cls = type(request)
        self._client.queue.put_nowait(
            StderrNext(text=f"pynixd: {req_cls.op} is not implemented\n")
        )
        return req_cls.response_type()
