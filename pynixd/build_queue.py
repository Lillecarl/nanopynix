"""
Build queue with deduplication and DAG-aware scheduling.

All builds go through this queue - the scheduler decides when and where
to execute them based on locality, DAG dependencies (via input_srcs),
and available slots.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import Self

import structlog

from .connection import ClientConn
from .operations.base import BuildResult, BuildResultStatus
from .operations.build_derivation import BuildDerivationRequest, BuildDerivationResponse
from .store_path import RequiredInput, StorePath

log = structlog.get_logger(__name__)


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
    request: BuildDerivationRequest  # The request to forward to the backend
    client: ClientConn | None  # Client connection for stderr forwarding
    required_paths: set[
        RequiredInput
    ]  # All paths the backend needs (input_srcs for BuildDerivation)
    future: asyncio.Future[BuildDerivationResponse]  # Resolved when done
    platform: str = ""  # Derivation platform (for backend filtering)
    enqueued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = field(default=None, repr=False)
    finished_at: float | None = field(default=None, repr=False)
    retries: int = 0
    failed_backends: list[str] = field(default_factory=list)
    build_task: asyncio.Task | None = field(default=None, repr=False)
    transfer_task: asyncio.Task | None = field(default=None, repr=False)
    transfer_cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    closure: set[StorePath] | None = field(
        default=None, repr=False
    )  # cached runtime closure

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

    drv_path: StorePath
    input_srcs: frozenset[StorePath] = frozenset()

    @classmethod
    def from_request(cls, request: BuildDerivationRequest) -> Self:
        return cls(
            drv_path=request.drv_path,
            input_srcs=frozenset(request.derivation.input_srcs),
        )


class BuildQueue:
    """Global queue for build operations with deduplication."""

    def __init__(self) -> None:
        self.queue: list[QueuedBuild] = []
        self.by_key: dict[BuildKey, QueuedBuild] = {}  # For deduplication
        self.next_id: int = 1
        self.lock: asyncio.Lock = asyncio.Lock()

    async def enqueue(
        self,
        request: BuildDerivationRequest,
        client: ClientConn | None,
        required_paths: set[RequiredInput],
        platform: str = "",
    ) -> tuple[int, asyncio.Future[BuildDerivationResponse]]:
        """Add a build to the queue (deduplicates if already present).

        Returns (build_id, future) - caller awaits the future for the response.
        Dedup only applies to builds that are still pending (not building/done).
        """
        key = BuildKey.from_request(request)

        async with self.lock:
            # Check for duplicate — dedup with queued or in-progress builds
            if key in self.by_key:
                existing = self.by_key[key]
                if not existing.is_done:
                    log.debug("build_deduped", id=existing.id)
                    return existing.id, existing.future
                # else: done, create new entry

            # Create new build with future
            loop = asyncio.get_event_loop()
            future: asyncio.Future[BuildDerivationResponse] = loop.create_future()
            build = QueuedBuild(
                id=self.next_id,
                request=request,
                client=client,
                required_paths=required_paths,
                future=future,
                platform=platform,
            )
            self.next_id += 1
            heapq.heappush(self.queue, build)
            self.by_key[key] = build

            log.info(
                "build_enqueued",
                build_id=build.id,
                description=build.description,
                required_paths=len(required_paths),
            )
            return build.id, future

    async def get_pending(self) -> list[QueuedBuild]:
        """Get all non-done builds sorted by ID."""
        async with self.lock:
            return sorted(
                [b for b in self.queue if not b.is_done],
                key=lambda b: b.id,
            )

    async def complete(
        self,
        build_id: int,
        response: BuildDerivationResponse,
    ) -> ClientConn | None:
        """Mark build as completed, resolve the future.

        Returns the client connection for the caller to use.
        """
        async with self.lock:
            for b in self.queue:
                if b.id == build_id:
                    b.finished_at = time.monotonic()
                    b.future.set_result(response)
                    log.info("build_completed", build_id=build_id)
                    return b.client
        raise ValueError(f"Build {build_id} not found")

    async def fail(self, build_id: int, error_msg: str) -> ClientConn | None:
        """Mark build as failed, resolve future with an error response.

        Returns the client connection for the caller to use.
        """
        async with self.lock:
            for b in self.queue:
                if b.id == build_id:
                    b.finished_at = time.monotonic()
                    response = BuildDerivationResponse(
                        result=BuildResult(
                            status=BuildResultStatus.MISC_FAILURE, error_msg=error_msg
                        ),
                    )
                    b.future.set_result(response)
                    log.info("build_failed", build_id=build_id, error_msg=error_msg)
                    return b.client
        raise ValueError(f"Build {build_id} not found")

    async def cleanup_completed(self) -> int:
        """Remove completed builds from queue, return count removed."""
        async with self.lock:
            before = len(self.queue)
            self.queue = [b for b in self.queue if not b.is_done]
            # Rebuild by_key
            self.by_key = {BuildKey.from_request(b.request): b for b in self.queue}
            return before - len(self.queue)
