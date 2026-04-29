"""Queued build tracking for the scheduler."""

from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

import structlog

from . import metrics
from .operations.build_derivation import BuildDerivationResponse
from .types.build import BuildResult, BuildResultStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .connection import ClientConn
    from .derived_path import DerivedPath
    from .operations.build_derivation import BuildDerivationRequest
    from .store_path import StorePath
    from .types.build import BuildMode
    from .types.path_info import UnkeyedValidPathInfo

log = structlog.get_logger(__name__)

MAX_STORE_RETRIES = 3


@dataclass(frozen=True)
class BuildKey:
    """Unique key for a build based on drv_path and input sources.
    Used for deduplication.
    """

    drv_path: StorePath
    input_srcs: frozenset[StorePath]

    @classmethod
    def from_request(cls, request: BuildDerivationRequest) -> Self:
        return cls(
            drv_path=request.drv_path,
            input_srcs=frozenset(request.derivation.input_srcs),
        )


@dataclass
class SchedulerBuildRequest:
    """Tracks the full lifecycle of a build_derived_paths() call.

    Individual QueuedBuilds come and go (completing, spawning trampoline
    derivations, etc.), but this request future stays until all paths
    the client asked for are satisfied.
    """

    id: int
    derived_paths: set[DerivedPath]
    build_mode: BuildMode
    client: ClientConn | None
    future: asyncio.Future[dict[DerivedPath, BuildResult]]
    active_build_ids: set[int] = field(default_factory=set)
    all_build_ids: set[int] = field(default_factory=set)
    build_to_derived: dict[int, set[DerivedPath]] = field(default_factory=dict)
    results: dict[DerivedPath, BuildResult] = field(default_factory=dict)

    def add_build(self, build_id: int, derived_paths: set[DerivedPath]) -> None:
        """Track a new build as part of this request."""
        self.active_build_ids.add(build_id)
        self.all_build_ids.add(build_id)
        self.build_to_derived[build_id] = derived_paths

    def build_completed(self, build_id: int) -> bool:
        """Remove build from active set. Returns True if request is complete."""
        self.active_build_ids.discard(build_id)
        return not self.active_build_ids

    def resolve_if_done(self) -> bool:
        """Resolve the future if all active builds are done."""
        if not self.active_build_ids and not self.future.done():
            self.future.set_result(dict(self.results))
            return True
        return False


@dataclass
class QueuedBuild:
    """State for a single derivation build task.

    Lifecycle:
    - is_pending: queued but not assigned
    - is_building: task spawned and running
    - is_done: future resolved
    """

    id: int  # Global incrementing ID
    request: BuildDerivationRequest  # The request to forward to the backend
    client: ClientConn | None  # Client connection for stderr forwarding
    required_paths: dict[StorePath, UnkeyedValidPathInfo]
    # All paths the backend needs (input_srcs for BuildDerivation)
    future: asyncio.Future[BuildDerivationResponse]  # Resolved when done
    platform: str = ""  # Derivation platform (for backend filtering)
    expected_duration: int | None = None  # Predicted duration in ms from DB
    enqueued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = field(default=None, repr=False)
    finished_at: float | None = field(default=None, repr=False)
    retries: int = 0
    store_failures: dict[str, int] = field(default_factory=dict)
    build_task: asyncio.Task | None = field(default=None, repr=False)

    # Build DAG: build IDs this build depends on (must complete before this
    # can be scheduled). Populated during decomposition for CA dependency
    # ordering.
    depends_on: set[int] = field(default_factory=set)

    # CA realisations from this build's outputs, populated after successful
    # build completion. Used to register realisations on builder stores
    # before building dependent (deferred) derivations.
    ca_realisations: list[dict] = field(default_factory=list, repr=False)

    # The store that was assigned to execute this build, set when
    # execute_build begins.
    assigned_store_id: str | None = field(default=None)

    # Back-reference to the SchedulerBuildRequest this build belongs to.
    # Set when the build is enqueued via build_derived_paths().
    # None for standalone build_derivation() calls.
    scheduler_request_id: int | None = field(default=None)

    # Dynamic input derivations from DrvWithVersion .drv files.
    # {drv_path: {output_name: [nested_output_name, ...], ...}}
    # Used by the trampoline to add depends_on edges and required_paths
    # to this build when a dynamic dep's inner build is enqueued.
    dynamic_input_drvs: dict[StorePath, dict[str, list[str]]] = field(
        default_factory=dict,
        repr=False,
    )

    # For heap ordering
    def __lt__(self, other: Self) -> bool:
        return self.id < other.id

    @property
    def is_building(self) -> bool:
        return self.build_task is not None and not self.build_task.done()

    @property
    def is_done(self) -> bool:
        return self.future.done()

    @property
    def is_pending(self) -> bool:
        return not self.is_building and not self.is_done

    def is_blacklisted(self, store_id: str) -> bool:
        """Check if a specific store has exceeded the retry limit for this build."""
        return self.store_failures.get(store_id, 0) >= MAX_STORE_RETRIES

    def reset_for_retry(self, failed_store_id: str) -> None:
        """Reset state for retry on next scheduling pass.

        Called after a build or infrastructure failure. The build will
        be re-scheduled to a different store if available, or the same
        store if it hasn't exceeded the per-backend retry limit.
        """
        self.retries += 1
        self.started_at = None
        self.build_task = None

        # Increment failure count for this specific store
        self.store_failures[failed_store_id] = self.store_failures.get(failed_store_id, 0) + 1

    def reset_for_busy(self) -> None:
        """Reset state because store was temporarily busy/stressed.
        Does NOT increment retries or change failure counts.
        """
        self.build_task = None
        self.started_at = None

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
        """Total seconds between enqueue and finish."""
        if self.finished_at is None:
            return None
        return self.finished_at - self.enqueued_at

    @property
    def description(self) -> str:
        """A human-readable description of the build."""
        pname = self.request.derivation.env.get("pname", "unknown")
        version = self.request.derivation.env.get("version", "unknown")
        return f"{pname}-{version}"


class BuildQueue:
    """Global queue for build operations with deduplication."""

    def __init__(self) -> None:
        self._queue: list[QueuedBuild] = []
        self._by_key: dict[BuildKey, QueuedBuild] = {}  # For deduplication
        self._by_id: dict[int, QueuedBuild] = {}  # For DAG lookups
        self._requests: dict[int, SchedulerBuildRequest] = {}
        self.next_id: int = 1
        self.next_request_id: int = 1
        self.lock: asyncio.Lock = asyncio.Lock()

    @property
    def queue(self) -> Sequence[QueuedBuild]:
        """Read-only view of the current build queue."""
        return self._queue

    @property
    def by_id(self) -> Mapping[int, QueuedBuild]:
        """Read-only mapping of build_id to QueuedBuild."""
        return self._by_id

    @property
    def requests(self) -> Mapping[int, SchedulerBuildRequest]:
        """Read-only mapping of request_id to SchedulerBuildRequest."""
        return self._requests

    async def create_request(
        self,
        derived_paths: set[DerivedPath],
        build_mode: BuildMode,
        client: ClientConn | None,
    ) -> tuple[int, SchedulerBuildRequest]:
        """Create a SchedulerBuildRequest and return it."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[DerivedPath, BuildResult]] = loop.create_future()
        async with self.lock:
            req = SchedulerBuildRequest(
                id=self.next_request_id,
                derived_paths=derived_paths,
                build_mode=build_mode,
                client=client,
                future=future,
            )
            self.next_request_id += 1
            self._requests[req.id] = req
            return req.id, req

    async def enqueue(
        self,
        request: BuildDerivationRequest,
        client: ClientConn | None,
        required_paths: dict[StorePath, UnkeyedValidPathInfo],
        platform: str = "",
        expected_duration: int | None = None,
        scheduler_request_id: int | None = None,
        derived_paths_for_request: set[DerivedPath] | None = None,
    ) -> tuple[int, asyncio.Future[BuildDerivationResponse]]:
        """Add a build to the queue (deduplicates if already present).

        Returns (build_id, future) - caller awaits the future for the response.
        Dedup only applies to builds that are still pending (not building/done).

        If scheduler_request_id is set, the build is tracked as part of that
        SchedulerBuildRequest. derived_paths_for_request maps this build to
        the original DerivedPaths it satisfies.
        """
        key = BuildKey.from_request(request)

        async with self.lock:
            # Check for duplicate — dedup with queued or in-progress builds
            if key in self._by_key:
                existing = self._by_key[key]
                if not existing.is_done:
                    log.debug("build_deduped", id=existing.id)
                    if scheduler_request_id is not None and existing.scheduler_request_id is None:
                        existing.scheduler_request_id = scheduler_request_id
                        if derived_paths_for_request:
                            sched_req = self._requests.get(scheduler_request_id)
                            if sched_req is not None:
                                sched_req.add_build(
                                    existing.id,
                                    derived_paths_for_request,
                                )
                    return existing.id, existing.future
                # else: done, create new entry

            # Create new build with future
            loop = asyncio.get_running_loop()
            future: asyncio.Future[BuildDerivationResponse] = loop.create_future()
            build = QueuedBuild(
                id=self.next_id,
                request=request,
                client=client,
                required_paths=required_paths,
                future=future,
                platform=platform,
                expected_duration=expected_duration,
                scheduler_request_id=scheduler_request_id,
            )
            self.next_id += 1
            heapq.heappush(self._queue, build)
            self._by_key[key] = build
            self._by_id[build.id] = build

            if scheduler_request_id is not None and derived_paths_for_request:
                sched_req = self._requests.get(scheduler_request_id)
                if sched_req is not None:
                    sched_req.add_build(build.id, derived_paths_for_request)

            log.info(
                "build_enqueued",
                build_id=build.id,
                description=build.description,
                required_paths=len(required_paths),
                scheduler_request_id=scheduler_request_id,
            )
            metrics.QUEUE_SIZE.labels(status="pending").inc()
            return build.id, future

    async def get_pending(self) -> list[QueuedBuild]:
        """Get all non-done builds sorted by ID."""
        async with self.lock:
            return sorted(
                [b for b in self._queue if not b.is_done],
                key=lambda b: b.id,
            )

    async def set_depends_on(self, build_id: int, depends_on: set[int]) -> None:
        """Set the build DAG dependencies for a build.

        Called after decomposition to link inter-drv dependencies.
        """
        async with self.lock:
            build = self._by_id.get(build_id)
            if build is not None:
                build.depends_on = depends_on

    async def complete(
        self,
        build_id: int,
        response: BuildDerivationResponse,
    ) -> ClientConn | None:
        """Mark build as completed, resolve the future.

        Returns the client connection for the caller to use.
        """
        async with self.lock:
            for b in self._queue:
                if b.id == build_id:
                    b.finished_at = time.monotonic()
                    if not b.future.done():
                        b.future.set_result(response)
                    log.info("build_completed", build_id=build_id)

                    metrics.QUEUE_SIZE.labels(status="building").dec()
                    metrics.QUEUE_SIZE.labels(status="done").inc()
                    status = "success" if response.result.status == BuildResultStatus.BUILT else "failure"
                    metrics.BUILDS_COMPLETED.labels(status=status).inc()
                    if b.build_time is not None:
                        metrics.BUILD_DURATION.observe(b.build_time)

                    return b.client
        raise ValueError(f"Build {build_id} not found")

    async def fail(self, build_id: int, error_msg: str) -> ClientConn | None:
        """Mark build as failed, resolve future with an error response.

        Returns the client connection for the caller to use.
        """
        async with self.lock:
            for b in self._queue:
                if b.id == build_id:
                    b.finished_at = time.monotonic()
                    response = BuildDerivationResponse(
                        result=BuildResult(
                            status=BuildResultStatus.MISC_FAILURE,
                            error_msg=error_msg,
                        ),
                    )
                    if not b.future.done():
                        b.future.set_result(response)
                    log.info("build_failed", build_id=build_id, error_msg=error_msg)

                    metrics.QUEUE_SIZE.labels(status="pending").dec()
                    metrics.QUEUE_SIZE.labels(status="done").inc()
                    metrics.BUILDS_COMPLETED.labels(status="failure").inc()

                    return b.client
        raise ValueError(f"Build {build_id} not found")

    async def cleanup_completed(self) -> int:
        """Remove completed builds from queue, return count removed."""
        async with self.lock:
            completed = [b for b in self._queue if b.is_done]
            removed_count = len(completed)
            if removed_count == 0:
                return 0

            self._queue = [b for b in self._queue if not b.is_done]
            # Rebuild by_key and by_id
            self._by_key = {BuildKey.from_request(b.request): b for b in self._queue}
            self._by_id = {b.id: b for b in self._queue}

            metrics.QUEUE_SIZE.labels(status="done").dec(removed_count)
            return removed_count

    def count(self, status: str) -> int:
        """Get count of builds with given status (non-async, thread-safe for reading)."""
        match status:
            case "pending":
                return len([b for b in self._queue if b.is_pending])
            case "running":
                return len([b for b in self._queue if b.is_building])
            case "done":
                return len([b for b in self._queue if b.is_done])
        return 0
