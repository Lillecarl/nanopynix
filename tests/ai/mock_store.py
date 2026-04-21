from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog

from pynixd.psi import CpuUtil
from pynixd.store import Store
from pynixd.store_path import StorePath

if TYPE_CHECKING:
    from pynixd.connection import ClientConn, Connection
    from pynixd.operations.base import OpRequest, Resp

log = structlog.get_logger(__name__)


class MockConnection:
    """Mock connection that delegates call() to the MockStore."""

    def __init__(self, store: MockStore) -> None:
        self.store = store
        self.version = 0x125  # Simulation version

    async def __aenter__(self) -> MockConnection:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
    ) -> Resp:
        return await self.store.call(
            request,
            client=client,
            suppress_last=suppress_last,
            raise_on_error=raise_on_error,
        )


class MockStore(Store):
    """A deterministic mock store for scheduler testing.

    Overrides I/O methods to return pre-configured responses and
    simulate virtual path tracking.
    """

    def __init__(
        self,
        id: str,
        max_builds: int = 2,
        max_transfers: int = 16,
        feature_matrix: dict[str, set[str]] | None = None,
        cpu_utilization: float = 0.0,
    ) -> None:
        # Pass probe=False to avoid real build-based system probing
        super().__init__(
            id=id,
            store_path=Path(f"/mock/{id}"),
            max_builds=max_builds,
            max_transfers=max_transfers,
            feature_matrix=feature_matrix,
            probe=False,
        )
        self.nix_version = "pynixd-mock-2.18.1"
        self.responses: dict[type[OpRequest], Any] = {}
        self.call_handlers: dict[type[OpRequest], Any] = {}
        self.build_blockers: dict[str, asyncio.Event] = {}
        self._cpu_util = CpuUtil(
            utilization=cpu_utilization, cores=4.0, throttled_pct=0.0
        )

    def block_build(self, drv_path: str | StorePath) -> asyncio.Event:
        """Prevent build from completing until the returned event is set."""
        event = asyncio.Event()
        self.build_blockers[str(drv_path)] = event
        return event

    @property
    def cpu_util(self) -> CpuUtil | None:
        return self._cpu_util

    @cpu_util.setter
    def cpu_util(self, value: CpuUtil | None) -> None:
        self._cpu_util = value

    async def create_conn(self) -> Connection:
        """Required by Store ABC but not used for MockStore."""
        raise NotImplementedError("MockStore does not use real connections")

    def build_conn(self) -> AsyncIterator[MockConnection]:
        """Override to return MockConnection instead of pooling."""

        class AsyncGen:
            def __init__(self, store: MockStore):
                self.store = store

            async def __aenter__(self):
                await self.store.build_semaphore.acquire()
                return MockConnection(self.store)

            async def __aexit__(self, *args):
                self.store.build_semaphore.release()

        return AsyncGen(self)  # type: ignore

    def transfer_conn(self) -> AsyncIterator[MockConnection]:
        """Override to return MockConnection instead of pooling."""

        class AsyncGen:
            def __init__(self, store: MockStore):
                self.store = store

            async def __aenter__(self):
                await self.store.transfer_semaphore.acquire()
                return MockConnection(self.store)

            async def __aexit__(self, *args):
                self.store.transfer_semaphore.release()

        return AsyncGen(self)  # type: ignore

    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
        skip_probe: bool = False,
    ) -> Resp:
        """Return a pre-configured response or run a handler."""
        req_type = type(request)

        # For builds, check if we need to block
        from pynixd.operations.build_derivation import BuildDerivationRequest

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

        log.warning(
            "mock_store_no_response", store_id=self.id, request=req_type.__name__
        )
        raise NotImplementedError(
            f"MockStore {self.id} has no response for {req_type.__name__}"
        )

    @classmethod
    async def stream_paths_store_to_store(
        cls,
        src: Store,
        dst: Store,
        paths: Iterable[StorePath],
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Instantly move paths between trackers to simulate transfer."""
        dst.tracker.add_known_paths(paths)
        log.debug(
            "mock_path_transfer_complete",
            src=src.id,
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
        """Execute request using our call logic (bypassing real fast-paths)."""
        return await self.call(request, client, suppress_last, skip_probe=skip_probe)

    def supports_derivation(self, platform: str, features: set[str]) -> bool:
        """Expose feature_matrix for ranking logic."""
        if self._feature_matrix is None:
            return False
        supported_features = self._feature_matrix.get(platform)
        if supported_features is None:
            return False
        return features.issubset(supported_features)

    @property
    def is_healthy(self) -> bool:
        return True
