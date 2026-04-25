from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, cast

import structlog

from pynixd import metrics
from pynixd.connection import Connection
from pynixd.operations.base import ValidPathInfo
from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.operations.query_all_valid_paths import (
    QueryAllValidPathsRequest,
    QueryAllValidPathsResponse,
)
from pynixd.operations.query_closure_with_info import (
    QueryClosureWithInfoRequest,
    QueryClosureWithInfoResponse,
)
from pynixd.psi import CpuUtil
from pynixd.store import Store
from pynixd.store_path import StorePath
from pynixd.wire import NixReader, NixWriter

if TYPE_CHECKING:
    from pynixd.connection import ClientConn
    from pynixd.operations.base import OpRequest, Resp

log = structlog.get_logger(__name__)


class MockConnection(Connection):
    """A mock representation of a Nix daemon connection.

    Instead of communicating over a real Unix or TCP socket, this class
    delegates all `call()` invocations directly back to the `MockStore`
    that created it. This allows tests to simulate the protocol handshake
    and operation execution without any network/IO overhead.
    """

    def __init__(self, store: MockStore) -> None:
        # We don't call super().__init__ because we don't have real R/W pairs.
        # Instead, we initialize the fields expected by the base class.
        self.store = store
        self.id = f"mock-{store.id}"
        self.version = 0x125  # Simulates a modern Nix protocol version (e.g. 1.37)
        self.nix_version = store.nix_version
        self.features = set()
        self.connected = True
        self.dirty = False
        self.op_log = []

        # Dummy reader/writer to avoid AttributeErrors on .identifier
        class DummyRW:
            def __init__(self, id: str):
                self.identifier = id

            def framed(self):
                return self

            def write(self, *args, **kwargs):
                pass

            def write_uint64(self, *args, **kwargs):
                pass

            def write_string(self, *args, **kwargs):
                pass

            def write_string_set(self, *args, **kwargs):
                pass

            async def is_dirty(self):
                return False

            async def drain(self):
                pass

            async def finalize(self):
                pass

        self.r = cast(NixReader, DummyRW(f"{self.id}-r"))
        self.w = cast(NixWriter, DummyRW(f"{self.id}-w"))

    async def __aenter__(self) -> MockConnection:
        """Simulate the entry into a connection context (e.g. acquiring from pool)."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Simulate connection release."""
        pass

    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
    ) -> Resp:
        """Forward the operation request to the MockStore for a response."""
        return await self.store.call(
            request,
            client=client,
            suppress_last=suppress_last,
            raise_on_error=raise_on_error,
        )


class MockStore(Store):
    """A high-fidelity deterministic mock of the pynixd `Store` class.

    This class allows for zero-I/O testing of the Scheduler and its
    associated components by virtualizing all interactions with the
    Nix daemon and the local filesystem.

    Key Features:
    - **Configurable Responses**: Set fixed responses for specific `OpRequest` types
      using the `.responses` dictionary.
    - **Dynamic Handlers**: Provide async functions in `.call_handlers` to
      generate responses dynamically based on the request content.
    - **Path Virtualization**: The `.tracker` (PathTracker) behaves exactly like
      the real one, but transfers are simulated as instant set operations.
    - **Build Control**: Use `.block_build(drv_path)` to pause a build in-flight
      and manually trigger its completion via an `asyncio.Event`.
    - **Load Simulation**: Tweak `cpu_util` at runtime to observe how the
      scheduler reacts to fleet saturation.
    """

    def __init__(
        self,
        id: str,
        feature_matrix: dict[str, set[str]] | None = None,
        cpu_utilization: float = 0.0,
    ) -> None:
        # We pass probe=False to disable the background task that normally
        # tries to run 'nix show-derivation' on real stores.
        super().__init__(
            id=id,
            store_path=Path(f"/mock/{id}"),
            feature_matrix=feature_matrix,
            probe=False,
        )
        self.nix_version = "pynixd-mock-2.18.1"
        # responses: Maps Request type -> fixed Response object
        self.responses: dict[type[OpRequest], Any] = {}
        # call_handlers: Maps Request type -> async handler function
        self.call_handlers: dict[type[OpRequest], Any] = {}
        # build_blockers: Maps drv_path -> asyncio.Event to control build timing
        self.build_blockers: dict[str, asyncio.Event] = {}
        # Initialize PSI-like load stats
        self._cpu_util = CpuUtil(
            utilization=cpu_utilization,
            cores=4.0,
            throttled_pct=0.0,
        )
        # In MockStore, we don't want real PSI monitoring, so we don't start any poller.

    def block_build(
        self,
        drv_path: str | StorePath,
        blocker: asyncio.Event | None = None,
    ) -> asyncio.Event:
        """Prevent a build of the given .drv from completing.

        The next `call(BuildDerivationRequest)` for this path will await
        the returned event. This is essential for testing concurrency and
        ensuring that the scheduler doesn't over-subscribe builders.
        """
        event = blocker or asyncio.Event()
        self.build_blockers[str(drv_path)] = event
        return event

    @property
    def cpu_util(self) -> CpuUtil | None:
        """Returns the current simulated CPU utilization."""
        return self._cpu_util

    @cpu_util.setter
    def cpu_util(self, value: CpuUtil | None) -> None:
        """Update the simulated load at runtime."""
        self._cpu_util = value

    async def create_conn(self) -> Connection:
        """Return a MockConnection."""
        return MockConnection(self)

    @contextlib.asynccontextmanager
    async def build_conn(self) -> AsyncIterator[MockConnection]:
        """Simulate acquiring a build connection."""
        async with self.pool.acquire("build"):
            yield MockConnection(self)

    @contextlib.asynccontextmanager
    async def transfer_conn(self) -> AsyncIterator[MockConnection]:
        """Simulate acquiring a transfer connection."""
        async with self.pool.acquire("transfer"):
            yield MockConnection(self)

    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
        skip_probe: bool = False,
    ) -> Resp:
        """Mocked Nix RPC call implementation.

        Logic flow:
        1. If it's a build request, check for registered blockers.
        2. Check for a custom handler in `call_handlers`.
        3. Check for a fixed response in `responses`.
        4. Otherwise, raise NotImplementedError to alert the test writer.
        """
        req_type = type(request)

        if isinstance(request, BuildDerivationRequest):
            drv_path = str(request.drv_path)
            if drv_path in self.build_blockers:
                log.debug("mock_build_blocking", store_id=self.id, drv_path=drv_path)
                await self.build_blockers[drv_path].wait()
                log.debug("mock_build_resuming", store_id=self.id, drv_path=drv_path)

        if req_type in self.call_handlers:
            return await self.call_handlers[req_type](request)

        if req_type in self.responses:
            return self.responses[req_type]

        # Default handlers for discovery ops
        if isinstance(request, QueryAllValidPathsRequest):
            return cast(
                Resp,
                QueryAllValidPathsResponse(paths=self.tracker.known_paths),
            )

        if isinstance(request, QueryClosureWithInfoRequest):
            infos = []
            for p in request.paths:
                # Provide a fake info for any requested path
                infos.append(
                    ValidPathInfo(
                        path=p,
                        deriver=StorePath(""),
                        nar_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
                        references=set(),
                        registration_time=0,
                        nar_size=1024,
                        ultimate=0,
                        sigs=set(),
                        ca="",
                    ),
                )
            return cast(Resp, QueryClosureWithInfoResponse(infos=infos))

        log.warning(
            "mock_store_no_response",
            store_id=self.id,
            request=req_type.__name__,
        )
        raise NotImplementedError(
            f"MockStore {self.id} has no response for {req_type.__name__}. "
            f"Update your test setup to provide a mock response.",
        )

    async def stream_paths_to(
        self,
        dst: Store,
        paths: Iterable[StorePath],
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Instantly move paths from this store to dst tracker.

        Bypasses NAR generation and socket streaming. The destination store's
        `PathTracker` is updated immediately.
        """
        dst.tracker.add_known_paths(paths)
        metrics.PATHS_TRANSFERRED.labels(source=self.id, destination=dst.id).inc(
            len(list(paths)),
        )
        log.debug(
            "mock_path_transfer_complete",
            src=self.id,
            dst=dst.id,
            count=len(list(paths)),
        )

    async def execute(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        skip_probe: bool = False,
    ) -> Resp:
        """A simple proxy to `call()`, bypassing any real SQLite or cache fast-paths."""
        return await self.call(request, client, suppress_last, skip_probe=skip_probe)

    def supports_derivation(
        self,
        system: str,
        features: set[str] | None = None,
    ) -> bool:
        """Check if the simulated store supports a specific system/feature set."""
        if self._feature_matrix is None:
            return False
        supported_features = self._feature_matrix.get(system)
        if supported_features is None:
            return False
        if features is None:
            return True
        return features.issubset(supported_features)

    @property
    def is_healthy(self) -> bool:
        """Mock stores are always healthy in tests unless you override this."""
        return True
