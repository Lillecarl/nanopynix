"""
Build queue with deduplication and DAG-aware scheduling.

All builds go through this queue - the scheduler decides when and where
to execute them based on locality, DAG dependencies (via input_srcs),
and available slots.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from dataclasses import dataclass, field
from typing import Self

from .connection import ClientConn
from .operations.base import BuildResult, BuildResultStatus, OpResponse
from .operations.builds import BuildDerivationRequest, BuildDerivationResponse
from .protocol import Op

log: logging.Logger = logging.getLogger(__name__)


@dataclass
class QueuedBuild:
    """A build job in the queue.

    All builds are decomposed into individual BuildDerivationRequests
    before enqueueing. BuildPaths/BuildPathsWithResults are split in the
    proxy layer.

    State is derived from task fields rather than an explicit status enum:
    - is_pending: no build task started, not done
    - is_building: build_task running
    - is_transferring: transfer_task running
    - is_done: future resolved
    """

    id: int  # Global incrementing ID
    op: Op  # Always BuildDerivation
    request: BuildDerivationRequest  # The request to forward to the backend
    client: ClientConn  # Client connection for stderr forwarding
    required_paths: set[
        str
    ]  # All paths the backend needs (input_srcs for BuildDerivation)
    future: asyncio.Future[OpResponse]  # Resolved when done
    platform: str = ""  # Derivation platform (for backend filtering)
    enqueued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = field(default=None, repr=False)
    finished_at: float | None = field(default=None, repr=False)
    retries: int = 0
    failed_backends: list[str] = field(default_factory=list)
    build_task: asyncio.Task | None = field(default=None, repr=False)
    transfer_task: asyncio.Task | None = field(default=None, repr=False)
    transfer_cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    closure: set[str] | None = field(default=None, repr=False)  # cached runtime closure

    # For heap ordering
    def __lt__(self, other: Self) -> bool:
        return self.id < other.id

    @property
    def is_building(self) -> bool:
        return self.build_task is not None and not self.build_task.done()

    @property
    def is_transferring(self) -> bool:
        return self.transfer_task is not None and not self.transfer_task.done()

    @property
    def is_done(self) -> bool:
        return self.future.done()

    @property
    def is_pending(self) -> bool:
        return not self.is_building and not self.is_done

    async def stop_transfer(self) -> None:
        """Signal proactive transfer to stop after current path and wait.

        Uses an Event rather than task.cancel() so we finish the in-flight
        NAR transfer before stopping. Cancelling mid-stream would leave the
        worker store in an inconsistent state — the store would have a partial
        path that it thinks is valid but isn't complete.
        """
        if self.is_transferring:
            self.transfer_cancel.set()
            assert self.transfer_task is not None
            await self.transfer_task

    def reset_for_retry(
        self, failed_store_id: str, old_transfer_task: asyncio.Task | None
    ) -> None:
        """Reset state for retry on next scheduling pass.

        Called after a build or infrastructure failure. The build will
        be re-scheduled to a different store if available.

        old_transfer_task: the task that stop_transfer() was awaiting.
        Only clears transfer_task if no new transfer was started in the gap.
        """
        self.retries += 1
        self.failed_backends.append(failed_store_id)
        self.build_task = None
        self.started_at = None
        # Don't kill a new transfer that started while stop_transfer() was awaiting
        if self.transfer_task is old_transfer_task:
            self.transfer_task = None
        self.transfer_cancel = asyncio.Event()

    @property
    def wait_time(self) -> float | None:
        """Seconds between enqueue and build start."""
        if self.started_at is None:
            return None
        return self.started_at - self.enqueued_at

    @property
    def build_time(self) -> float | None:
        """Seconds between build start and finish."""
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    @property
    def total_time(self) -> float | None:
        """Seconds between enqueue and finish."""
        if self.finished_at is None:
            return None
        return self.finished_at - self.enqueued_at

    @property
    def description(self) -> str:
        """Short description for logging."""
        return self.request.drv_path


@dataclass(frozen=True)
class BuildKey:
    """Key for deduplication."""

    drv_path: str
    input_srcs: frozenset[str] = frozenset()

    @classmethod
    def from_request(cls, request: BuildDerivationRequest) -> Self:
        return cls(
            drv_path=request.drv_path,
            input_srcs=frozenset(request.derivation.input_srcs),
        )


class BuildQueue:
    """Global queue for build operations with deduplication."""

    def __init__(self) -> None:
        self._queue: list[QueuedBuild] = []
        self._by_key: dict[BuildKey, QueuedBuild] = {}  # For deduplication
        self._next_id: int = 1
        self._lock: asyncio.Lock = asyncio.Lock()

    async def enqueue(
        self,
        op: Op,
        request: BuildDerivationRequest,
        client: ClientConn,
        required_paths: set[str],
        platform: str = "",
    ) -> tuple[int, asyncio.Future[OpResponse]]:
        """Add a build to the queue (deduplicates if already present).

        Returns (build_id, future) - caller awaits the future for the response.
        Dedup only applies to builds that are still pending (not building/done).
        """
        key = BuildKey.from_request(request)

        async with self._lock:
            # Check for duplicate — dedup with queued or in-progress builds
            if key in self._by_key:
                existing = self._by_key[key]
                if not existing.is_done:
                    log.debug(
                        "Build deduped onto existing build ID %d",
                        existing.id,
                    )
                    return existing.id, existing.future
                # else: done, create new entry

            # Create new build with future
            loop = asyncio.get_event_loop()
            future: asyncio.Future[OpResponse] = loop.create_future()
            build = QueuedBuild(
                id=self._next_id,
                op=op,
                request=request,
                client=client,
                required_paths=required_paths,
                future=future,
                platform=platform,
            )
            self._next_id += 1
            heapq.heappush(self._queue, build)
            self._by_key[key] = build

            log.info(
                "Build %d enqueued: %s (%d required paths)",
                build.id,
                build.description,
                len(required_paths),
            )
            return build.id, future

    async def get_pending(self) -> list[QueuedBuild]:
        """Get all non-done builds sorted by ID."""
        async with self._lock:
            return sorted(
                [b for b in self._queue if not b.is_done],
                key=lambda b: b.id,
            )

    async def complete(
        self,
        build_id: int,
        response: OpResponse,
    ) -> ClientConn:
        """Mark build as completed, resolve the future.

        Returns the client connection for the caller to use.
        """
        async with self._lock:
            for b in self._queue:
                if b.id == build_id:
                    b.finished_at = time.monotonic()
                    b.future.set_result(response)
                    log.info("Build %d completed", build_id)
                    return b.client
        raise ValueError(f"Build {build_id} not found")

    async def fail(self, build_id: int, error_msg: str) -> ClientConn:
        """Mark build as failed, resolve future with an error response.

        Returns the client connection for the caller to use.
        """
        async with self._lock:
            for b in self._queue:
                if b.id == build_id:
                    b.finished_at = time.monotonic()
                    response: OpResponse = BuildDerivationResponse(
                        result=BuildResult(
                            status=BuildResultStatus.MISC_FAILURE, error_msg=error_msg
                        ),
                    )
                    b.future.set_result(response)
                    log.info("Build %d failed: %s", build_id, error_msg)
                    return b.client
        raise ValueError(f"Build {build_id} not found")

    async def cleanup_completed(self) -> int:
        """Remove completed builds from queue, return count removed."""
        async with self._lock:
            before = len(self._queue)
            self._queue = [b for b in self._queue if not b.is_done]
            # Rebuild by_key
            self._by_key = {BuildKey.from_request(b.request): b for b in self._queue}
            return before - len(self._queue)
