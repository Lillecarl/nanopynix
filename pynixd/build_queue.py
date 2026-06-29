"""Queued build tracking for the scheduler."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
import structlog

from . import metrics, wire
from .serde import BuildDerivationResponse, BuildMode, BuildResult, BuildResultStatus
from .serde.logs import LogMessage
from .types.context import WriteContext
from .types.ids import BuildId, RequestId, StoreId

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .connection import ClientConn
    from .derived_path import DerivedPath
    from .serde import BuildDerivationRequest, Realisation
log = structlog.get_logger(__name__)

MAX_STORE_RETRIES = 3


@dataclass
class SchedulerBuildRequest:
    """Tracks the full lifecycle of a build_derived_paths() call.

    Individual QueuedBuilds come and go (completing, spawning trampoline
    derivations, etc.), but this request future stays until all paths
    the client asked for are satisfied.
    """

    id: RequestId
    derived_paths: set[DerivedPath]
    build_mode: BuildMode
    client: ClientConn | None
    future: asyncio.Future[dict[DerivedPath, BuildResult]]
    active_build_ids: set[BuildId] = field(default_factory=set)
    all_build_ids: set[BuildId] = field(default_factory=set)
    build_to_derived: dict[BuildId, set[DerivedPath]] = field(default_factory=dict)
    results: dict[DerivedPath, BuildResult] = field(default_factory=dict)

    def add_build(self, build_id: BuildId, derived_paths: set[DerivedPath]) -> None:
        """Track a new build as part of this request."""
        self.active_build_ids.add(build_id)
        self.all_build_ids.add(build_id)
        self.build_to_derived[build_id] = derived_paths

    def build_completed(self, build_id: BuildId) -> bool:
        """Remove build from active set. Returns True if request is complete."""
        self.active_build_ids.discard(build_id)
        return not self.active_build_ids

    def resolve_if_done(self) -> bool:
        """Resolve the future if all active builds are done."""
        if not self.active_build_ids and not self.future.done():
            self.future.set_result(dict(self.results))
            return True
        return False


class QueuedBuild:
    """State for a single derivation build task.

    Lifecycle:
    - is_pending: queued but not assigned
    - is_building: task spawned and running
    - is_done: future resolved
    """

    def __init__(
        self,
        build_id: BuildId,
        request: BuildDerivationRequest,
        future: asyncio.Future[BuildDerivationResponse],
        expected_duration: int | None = None,
        scheduler_request_ids: set[RequestId] | None = None,
    ) -> None:
        self.build_id = build_id
        self.request = request
        self.future = future
        self.expected_duration = expected_duration
        self.enqueued_at = time.monotonic()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.retries = 0
        self.store_failures: dict[StoreId, int] = {}
        self.build_task: asyncio.Task[object] | None = None

        self.from_goal_path: bool = False

        # CA realisations from this build's outputs, populated after successful
        # build completion. Used to register realisations on builder stores
        # before building dependent (deferred) derivations.
        self.ca_realisations: list[Realisation] = []

        # The store that was assigned to execute this build, set when
        # execute_build begins.
        self.assigned_store_id: StoreId | None = None

        # BuildRequest(s) this build belongs to.
        # Multiple requests can share a single build (dedup).
        # Empty set for standalone build_derivation() calls.
        self.scheduler_request_ids: set[RequestId] = scheduler_request_ids or set()

        # Append-only byte buffer of all stderr messages (serialized via
        # NixWriter interface). New subscribers get this replayed on join.
        self._log_writer = wire.BytesWriter("build_log")

        # Client connections subscribed to this build's stderr stream.
        self.subscribers: list[ClientConn] = []

        # Guards add_subscriber replay vs post_log_bytes fanout so that a
        # joining client never misses bytes that arrive during catch-up.
        self._sub_lock = anyio.Lock()

    @property
    def is_building(self) -> bool:
        return self.build_task is not None and not self.build_task.done()

    @property
    def is_done(self) -> bool:
        return self.future.done()

    @property
    def is_pending(self) -> bool:
        return not self.is_building and not self.is_done

    def is_blacklisted(self, store_id: StoreId) -> bool:
        """Check if a specific store has exceeded the retry limit for this build."""
        return self.store_failures.get(store_id, 0) >= MAX_STORE_RETRIES

    def reset_for_retry(self, failed_store_id: StoreId) -> None:
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

    # ── Log pub/sub methods ───────────────────────────────────────────

    async def post_log(self, msg: LogMessage) -> bytes:
        """Serialize and store a log entry. Returns the raw bytes."""
        before = self._log_writer.tell()
        await msg.to_writer(WriteContext(writer=self._log_writer, version=wire.PROTOCOL_VERSION))
        return self._log_writer.get_bytes()[before:]

    async def _send_raw_safe(self, sub: ClientConn, raw: bytes) -> None:
        """Send raw bytes to a subscriber, removing it on failure."""
        try:
            await sub.send_raw(raw)
        except (OSError, BrokenPipeError, ConnectionResetError):
            async with self._sub_lock:
                self.subscribers.remove(sub)

    async def post_log_bytes(self, raw: bytes) -> None:
        """Fan out raw log bytes to all subscribers via TaskGroup.

        Dead subscribers (send failure) are removed automatically.
        """
        if not self.subscribers or not raw:
            return
        async with self._sub_lock:
            if not self.subscribers:
                return
            async with anyio.create_task_group() as tg:
                for sub in self.subscribers:
                    tg.start_soon(self._send_raw_safe, sub, raw)

    async def post_log_and_fanout(self, msg: LogMessage) -> None:
        """Store a log entry and fan out to all subscribers."""
        raw = await self.post_log(msg)
        await self.post_log_bytes(raw)

    async def add_subscriber(self, client: ClientConn) -> None:
        """Register a client to receive this build's logs.

        Replays the full logged history so far, then the client
        receives new entries in real-time via post_log_bytes.
        If replay fails (broken connection), the subscriber is not added.
        """
        async with self._sub_lock:
            if self._log_writer.tell():
                try:
                    await client.send_raw(self._log_writer.get_bytes())
                except (OSError, BrokenPipeError, ConnectionResetError):
                    log.debug("subscriber_replay_failed", build_id=self.build_id)
                    return
            self.subscribers.append(client)


class BuildQueue:
    """Global queue for build operations with deduplication."""

    def __init__(self) -> None:
        self._queue: list[QueuedBuild] = []
        self._by_path: dict[str, QueuedBuild] = {}  # drv_path -> build for dedup
        self._by_id: dict[BuildId, QueuedBuild] = {}  # For DAG lookups
        self._requests: dict[RequestId, SchedulerBuildRequest] = {}
        self.next_id = 1
        self.next_request_id = 1
        self.lock: anyio.Lock = anyio.Lock()

    @property
    def queue(self) -> Sequence[QueuedBuild]:
        """Read-only view of the current build queue."""
        return self._queue

    @property
    def by_id(self) -> Mapping[BuildId, QueuedBuild]:
        """Read-only mapping of build_id to QueuedBuild."""
        return self._by_id

    @property
    def by_path(self) -> Mapping[str, QueuedBuild]:
        """Read-only mapping of drv_path to QueuedBuild."""
        return self._by_path

    @property
    def requests(self) -> Mapping[RequestId, SchedulerBuildRequest]:
        """Read-only mapping of request_id to SchedulerBuildRequest."""
        return self._requests

    async def create_request(
        self,
        derived_paths: set[DerivedPath],
        build_mode: BuildMode,
        client: ClientConn | None,
    ) -> tuple[RequestId, SchedulerBuildRequest]:
        """Create a SchedulerBuildRequest and return it."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[DerivedPath, BuildResult]] = loop.create_future()
        async with self.lock:
            req_id = RequestId(self.next_request_id)
            req = SchedulerBuildRequest(
                id=req_id,
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
        expected_duration: int | None = None,
        scheduler_request_id: RequestId | None = None,
        derived_paths_for_request: set[DerivedPath] | None = None,
        from_goal_path: bool = False,
    ) -> tuple[BuildId, asyncio.Future[BuildDerivationResponse]]:
        """Add a build to the queue (deduplicates if already present).

        Returns (build_id, future) - caller awaits the future for the response.
        Dedup only applies to builds that are still pending (not building/done).

        If scheduler_request_id is set, the build is tracked as part of that
        SchedulerBuildRequest. derived_paths_for_request maps this build to
        the original DerivedPaths it satisfies.

        Deduplication uses drv_path as the identity — a .drv file is immutable,
        so the same drv_path always means the same build.
        """
        drv_path = str(request.drv_path)

        async with self.lock:
            # Check for duplicate — dedup with queued or in-progress builds
            existing = self._by_path.get(drv_path)
            if existing is not None and not existing.is_done:
                log.debug("build_deduped", id=existing.build_id)
                existing.from_goal_path = existing.from_goal_path or from_goal_path
                if scheduler_request_id is not None:
                    existing.scheduler_request_ids.add(scheduler_request_id)
                    if derived_paths_for_request:
                        sched_req = self._requests.get(scheduler_request_id)
                        if sched_req is not None:
                            sched_req.add_build(
                                existing.build_id,
                                derived_paths_for_request,
                            )
                return existing.build_id, existing.future

            # Create new build with future
            loop = asyncio.get_running_loop()
            future: asyncio.Future[BuildDerivationResponse] = loop.create_future()
            build_id = BuildId(self.next_id)
            scheduler_request_ids: set[RequestId] = (
                {scheduler_request_id} if scheduler_request_id is not None else set()
            )
            build = QueuedBuild(
                build_id=build_id,
                request=request,
                future=future,
                expected_duration=expected_duration,
                scheduler_request_ids=scheduler_request_ids,
            )
            build.from_goal_path = from_goal_path
            self.next_id += 1
            self._queue.append(build)
            self._by_path[drv_path] = build
            self._by_id[build.build_id] = build

            if scheduler_request_id is not None and derived_paths_for_request:
                sched_req = self._requests.get(scheduler_request_id)
                if sched_req is not None:
                    sched_req.add_build(build.build_id, derived_paths_for_request)

            log.info(
                "build_enqueued",
                build_id=build.build_id,
                description=build.description,
                request_ids=list(scheduler_request_ids),
            )
            metrics.QUEUE_SIZE.labels(status="pending").inc()
            return build.build_id, future

    async def subscribe(self, build_id: BuildId, client: ClientConn) -> bool:
        """Subscribe a client to a build's log stream.

        Returns True if the build was found and subscriber added.
        """
        async with self.lock:
            build = self._by_id.get(build_id)
            if build is None:
                return False
            await build.add_subscriber(client)
            return True

    async def get_pending(self) -> list[QueuedBuild]:
        """Get all non-done builds sorted by ID."""
        async with self.lock:
            return sorted(
                [b for b in self._queue if not b.is_done],
                key=lambda b: b.build_id,
            )

    async def complete(
        self,
        build_id: BuildId,
        response: BuildDerivationResponse,
    ) -> None:
        """Mark build as completed, resolve the future."""
        async with self.lock:
            for b in self._queue:
                if b.build_id == build_id:
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

                    return
        raise ValueError(f"Build {build_id} not found")

    async def fail(self, build_id: BuildId, error_msg: str) -> None:
        """Mark build as failed, resolve future with an error response."""
        async with self.lock:
            for b in self._queue:
                if b.build_id == build_id:
                    b.finished_at = time.monotonic()
                    response = BuildDerivationResponse(
                        result=BuildResult(
                            status=BuildResultStatus.MISC_FAILURE,
                            error_msg=error_msg,
                            times_built=0,
                            is_non_deterministic=0,
                            start_time=0,
                            stop_time=0,
                            built_outputs={},
                        ),
                    )
                    if not b.future.done():
                        b.future.set_result(response)
                    log.info("build_failed", build_id=build_id, error_msg=error_msg)

                    if b.is_building:
                        metrics.QUEUE_SIZE.labels(status="building").dec()
                    else:
                        metrics.QUEUE_SIZE.labels(status="pending").dec()
                    metrics.QUEUE_SIZE.labels(status="done").inc()
                    metrics.BUILDS_COMPLETED.labels(status="failure").inc()

                    return
        raise ValueError(f"Build {build_id} not found")

    async def prune_request(self, request_id: RequestId) -> None:
        """Remove completed builds from the queue if no other request references them.

        Called after a SchedulerBuildRequest resolves. Builds that are still
        referenced by other requests are kept; only the request id is removed
        from their set.
        """
        async with self.lock:
            to_remove: list[QueuedBuild] = []
            for build in self._queue:
                if request_id in build.scheduler_request_ids:
                    build.scheduler_request_ids.discard(request_id)
                    if not build.scheduler_request_ids and build.is_done:
                        to_remove.append(build)

            for build in to_remove:
                self._queue.remove(build)
                self._by_id.pop(build.build_id, None)
                drv_path_str = str(build.request.drv_path)
                if drv_path_str in self._by_path:
                    del self._by_path[drv_path_str]
                log.debug(
                    "build_pruned",
                    build_id=build.build_id,
                    drv_path=drv_path_str,
                )

            # Also remove the request itself
            self._requests.pop(request_id, None)

    def count(self, status: str) -> int:
        """Get count of builds with given status.

        Safe to call from async context (no await points, so no
        interleaving with queue mutations).
        """
        match status:
            case "pending":
                return len([b for b in self._queue if b.is_pending])
            case "running":
                return len([b for b in self._queue if b.is_building])
            case "done":
                return len([b for b in self._queue if b.is_done])
        return 0
