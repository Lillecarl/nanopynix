"""Mock Store implementations for unit and functional tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from pynixd import metrics, wire
from pynixd.config import StoreSpecBase
from pynixd.operations.base import OpResponse
from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.operations.query_all_valid_paths import (
    QueryAllValidPathsRequest,
    QueryAllValidPathsResponse,
)
from pynixd.operations.query_closure_with_info import (
    QueryClosureWithInfoRequest,
    QueryClosureWithInfoResponse,
)
from pynixd.operations.query_valid_paths import QueryValidPathsRequest, QueryValidPathsResponse
from pynixd.psi import CpuUtil
from pynixd.serde import StorePath as SerdeStorePath
from pynixd.serde.query_all_valid_paths import QueryAllValidPathsRequest as SerdeQueryAllValidPathsRequest, QueryAllValidPathsResponse as SerdeQueryAllValidPathsResponse
from pynixd.serde.wire_message import WireModel
from pynixd.store.daemon import DaemonStore
from pynixd.store_path import StorePath
from pynixd.types.ids import StoreId
from pynixd.types.path_info import ValidPathInfo

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pynixd.connection import ClientConn, Connection
    from pynixd.drv_parser import Derivation
    from pynixd.operations.base import OpRequest, Resp
    from pynixd.serde.wire_ops import WireRequest
    from pynixd.wire import NixReader, NixWriter


log = structlog.get_logger(__name__)


class MockConnection:
    """Mock connection that doesn't actually connect to anything.

    Delegates all `call()` invocations directly back to the `MockStore`
    that created it. This allows tests to simulate the protocol handshake
    and operation execution without any network/IO overhead.
    """

    def __init__(self, store: MockStore) -> None:
        # We don't call super().__init__ because we don't have real R/W pairs.
        # Instead, we initialize the fields expected by the base class.
        self.store = store
        self.id = f"mock-{store.store_id}"
        self.version = 0x125  # Simulates a modern Nix protocol version (e.g. 1.37)
        self.nix_version = store.nix_version
        self.features = set()
        self.connected = True
        self.dirty = False
        self.op_log = []

        # Dummy reader/writer to avoid AttributeErrors on .identifier
        class DummyRW:
            def __init__(self, rw_id: str):
                self.identifier = rw_id

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

        self.r = cast("NixReader", DummyRW(f"{self.id}-r"))
        self.w = cast("NixWriter", DummyRW(f"{self.id}-w"))

    async def __aenter__(self) -> MockConnection:
        """Simulate the entry into a connection context (e.g. acquiring from pool)."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Simulate connection release."""

    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
    ) -> Resp:
        """Forward all calls to the parent MockStore's execute_mock method."""
        return await self.store.execute_mock(request)

    def close(self):
        self.connected = False


class MockStore(DaemonStore):
    """Store with pre-recorded or dynamically generated responses.

    Useful for testing complex orchestration logic (like build decomposition
    or the build scheduler) without spawning real Nix processes.
    Also tracks statistics like current CPU utilization to test how the
    scheduler reacts to fleet saturation.
    """

    def __init__(
        self,
        store_id: str | StoreId,
        feature_matrix: dict[str, set[str]] | None = None,
        cpu_utilization: float = 0.0,
    ) -> None:
        spec = StoreSpecBase(
            store_id=StoreId(store_id),
            feature_matrix=feature_matrix,
            probe=False,
        )
        super().__init__(spec)
        self.store_path = Path(f"/mock/{store_id}")
        self.nix_version = "pynixd-mock-2.18.1"
        # responses: Maps Request type -> fixed Response object
        self.responses: dict[type[OpRequest], Any] = {}
        # call_handlers: Maps Request type -> async handler function
        self.call_handlers: dict[type[OpRequest], Any] = {}

        self.build_blockers: dict[str, asyncio.Event] = {}
        self.cpu_utilization_val = cpu_utilization

    @property
    def cpu_util(self) -> CpuUtil | None:
        """Mocked CPU utilization."""

        return CpuUtil(utilization=self.cpu_utilization_val, cores=1.0, throttled_pct=0.0)

    @property
    def pressure(self) -> float | None:
        return self.cpu_utilization_val

    def set_cpu_utilization(self, val: float) -> None:
        self.cpu_utilization_val = val

    def block_build(self, drv_path: str | StorePath, blocker: asyncio.Event | None = None) -> asyncio.Event:
        """Create or use an event that will block builds of this drv_path."""
        event = blocker or asyncio.Event()
        self.build_blockers[str(drv_path)] = event
        return event

    def unblock_build(self, drv_path: str | StorePath) -> None:
        """Signal the event to allow blocked builds of this drv_path to proceed."""
        drv_str = str(drv_path)
        if drv_str in self.build_blockers:
            self.build_blockers[drv_str].set()

    async def create_conn(self) -> Connection:
        """Return a MockConnection that delegates back to us."""
        return cast("Connection", MockConnection(self))

    async def execute_mock(self, request: OpRequest[Resp]) -> Resp:
        """Internal dispatcher for the MockConnection."""
        req_type = type(request)

        if isinstance(request, BuildDerivationRequest):
            drv_path = str(request.drv_path)
            if drv_path in self.build_blockers:
                log.debug("mock_build_blocking", store_id=self.store_id, drv_path=drv_path)
                await self.build_blockers[drv_path].wait()
                log.debug("mock_build_resuming", store_id=self.store_id, drv_path=drv_path)

        if req_type in self.call_handlers:
            return await self.call_handlers[req_type](request)

        if req_type in self.responses:
            return cast("Resp", self.responses[req_type])

        # Dynamic defaults for common queries
        if isinstance(request, QueryValidPathsRequest):
            return cast("Resp", QueryValidPathsResponse(paths=request.paths))

        if req_type == QueryAllValidPathsRequest or req_type == SerdeQueryAllValidPathsRequest:
            return cast("Resp", SerdeQueryAllValidPathsResponse(paths={SerdeStorePath(path=str(p)) for p in self.tracker.known_paths}))
        if isinstance(request, QueryClosureWithInfoRequest):
            # Just return some dummy info for everything

            infos = [
                ValidPathInfo(
                    path=p,
                    deriver=StorePath(""),
                    nar_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
                    nar_size=1024,
                    references=set(),
                    registration_time=0,
                    ultimate=1,
                    sigs=set(),
                    ca="",
                )
                for p in request.paths
            ]
            return cast("Resp", QueryClosureWithInfoResponse(infos=infos))

        log.warning(
            "mock_store_no_response",
            store_id=self.store_id,
            request=req_type.__name__,
        )
        raise NotImplementedError(
            f"MockStore {self.store_id} has no response for {req_type.__name__}. "
            f"Update your test setup to provide a mock response.",
        )

    async def read_derivation(self, drv_store_path: StorePath | str) -> Derivation | None:
        """Read a .drv file from the mock filesystem."""
        from pynixd.drv_parser import read_drv_file

        return await read_drv_file(self.store_path, drv_store_path)

    async def execute(  # type: ignore[override]
        self,
        request: OpRequest[Resp] | WireRequest,
        client: ClientConn | None = None,
        suppress_last: bool = False,
        skip_probe: bool = False,
    ) -> Any:
        """Always use the dynamic mock logic, bypassing request.execute()."""
        if isinstance(request, WireModel):
            return await self.call(request, client=client, suppress_last=suppress_last)
        # Record the operation for test verification
        async with self.transfer_conn() as conn:
            # We know it's a MockConnection which has op_log
            cast("Any", conn).op_log.append(type(request).__name__)

        return await self.execute_mock(request)
