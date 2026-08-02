"""Stdio worker pools: managed subprocess groups bridged by logical peers."""

from __future__ import annotations

import contextlib
import itertools
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

from grpclib_transports.bidi import LogicalRpcPeer
from grpclib_transports.inproc import (
    BackchannelServiceFactory as InprocBackchannelServiceFactory,
    ServiceFactory as InprocServiceFactory,
    inproc_worker,
    inproc_worker_with_backchannel,
)
from grpclib_transports.multiprocessing import (
    BackchannelServiceFactory,
    ServiceFactory,
    multiprocessing_worker,
    multiprocessing_worker_with_backchannel,
)
from grpclib_transports.protocol import DEFAULT_TUNING, TransportTuning
from grpclib_transports.stdio import (
    StdioChannel,
    stdio_worker,
    stdio_worker_with_backchannel,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Collection
    from pathlib import Path

    from grpclib._typing import IServable

PeerT = TypeVar("PeerT", bound=LogicalRpcPeer)
PeerFactory = Callable[[StdioChannel], Awaitable[PeerT]]
ClientT = TypeVar("ClientT")
ClientFactory = Callable[[Any], ClientT]


@dataclass(frozen=True)
class RegisteredPeer[PeerT: LogicalRpcPeer]:
    """A :class:`LogicalRpcPeer` registered with an ID and optional metadata.

    Delegates :meth:`call` and :meth:`event` to the wrapped peer.
    """

    id: str
    peer: PeerT
    metadata: Mapping[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType] -- dict() satisfies Mapping[str, Any] at runtime

    async def call(
        self,
        method: str,
        payload: Any = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- the deadline belongs to the RPC, not to the caller's scope: on expiry this sends a `cancel` frame to the peer, which an `anyio.fail_after` around the call cannot do -- that would abandon the local future and leave the remote handler running.
    ) -> Any:
        return await self.peer.call(method, payload, timeout=timeout)

    async def event(self, method: str, payload: Any = None) -> None:
        await self.peer.event(method, payload)


class PeerRegistry[PeerT: LogicalRpcPeer]:
    """A thread-unsafe registry of :class:`RegisteredPeer` instances.

    Supports :func:`len`, iteration, and snapshot via :meth:`snapshot`.
    Broadcast calls to all registered peers with :meth:`call_all`.
    """

    def __init__(self) -> None:
        self._next_id = itertools.count(1)
        self._peers: dict[str, RegisteredPeer[PeerT]] = {}

    def __len__(self) -> int:
        return len(self._peers)

    def __iter__(self) -> Iterator[RegisteredPeer[PeerT]]:
        return iter(self.snapshot())

    def register(
        self,
        peer: PeerT,
        *,
        peer_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RegisteredPeer[PeerT]:
        resolved_id = peer_id or f"peer-{next(self._next_id)}"
        if resolved_id in self._peers:
            raise ValueError(f"peer {resolved_id!r} is already registered")
        registered = RegisteredPeer(
            id=resolved_id,
            peer=peer,
            metadata=metadata or {},
        )
        self._peers[resolved_id] = registered
        return registered

    def unregister(self, peer_id: str) -> RegisteredPeer[PeerT] | None:
        return self._peers.pop(peer_id, None)

    def get(self, peer_id: str) -> RegisteredPeer[PeerT] | None:
        return self._peers.get(peer_id)

    def snapshot(self) -> tuple[RegisteredPeer[PeerT], ...]:
        return tuple(self._peers.values())

    async def call_all(
        self,
        method: str,
        payload: Any = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- the deadline belongs to the RPC, not to the caller's scope: on expiry this sends a `cancel` frame to the peer, which an `anyio.fail_after` around the call cannot do -- that would abandon the local future and leave the remote handler running.
    ) -> list[Any]:
        return [await peer.call(method, payload, timeout=timeout) for peer in self.snapshot()]

    async def aclose(self) -> None:
        for registered in self.snapshot():
            await registered.peer.aclose()
            self.unregister(registered.id)


class StdioPeerPool[PeerT: LogicalRpcPeer]:
    """A pool of *size* subprocess workers, each bridged by a :class:`LogicalRpcPeer`.

    Use as an async context manager.  On enter, spawns *size* child processes
    via :func:`~grpclib_transports.stdio.stdio_worker`, creates peers with
    *peer_factory*, and registers them in :attr:`registry`.  On exit, closes
    all peers and terminates all subprocesses.
    """

    def __init__(
        self,
        argv: Sequence[str | Path],
        *,
        peer_factory: PeerFactory[PeerT],
        size: int = 1,
        tuning: TransportTuning = DEFAULT_TUNING,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stderr: Any = None,
    ) -> None:
        if size <= 0:
            raise ValueError("size must be positive")
        self._argv = argv
        self._peer_factory = peer_factory
        self._size = size
        self._tuning = tuning
        self._cwd = cwd
        self._env = env
        self._stderr = stderr
        self._parent_services = ()
        self._stack = contextlib.AsyncExitStack()
        self.registry: PeerRegistry[PeerT] = PeerRegistry()

    def __len__(self) -> int:
        return len(self.registry)

    def __iter__(self) -> Iterator[RegisteredPeer[PeerT]]:
        return iter(self.registry)

    async def __aenter__(self) -> StdioPeerPool[PeerT]:
        for index in range(self._size):
            channel = await self._stack.enter_async_context(self._worker_manager())
            peer = await self._peer_factory(channel)
            peer.start()
            self.registry.register(
                peer,
                peer_id=f"stdio-{index + 1}",
                metadata={"transport": "stdio", "index": index},
            )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.registry.aclose()
        await self._stack.aclose()

    def with_parent_services(
        self,
        parent_services: Collection[IServable],
    ) -> StdioPeerPool[PeerT]:
        self._parent_services = tuple(parent_services)
        return self

    def _worker_manager(self) -> Any:
        if self._parent_services:
            return stdio_worker_with_backchannel(
                self._argv,
                self._parent_services,
                tuning=self._tuning,
                cwd=self._cwd,
                env=self._env,
                stderr=self._stderr,
            )
        return stdio_worker(
            self._argv,
            tuning=self._tuning,
            cwd=self._cwd,
            env=self._env,
            stderr=self._stderr,
        )


@dataclass(frozen=True)
class ManagedWorker[ClientT = Any]:
    """A managed worker channel and optional typed client."""

    id: str
    channel: Any
    client: ClientT
    metadata: Mapping[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType] -- dict() satisfies Mapping[str, Any] at runtime


class WorkerPool[ClientT = Any]:
    """A pool of managed worker channels.

    Use as an async context manager. Worker transports are entered through
    an internal :class:`contextlib.AsyncExitStack`, so channels and child
    processes are closed when the pool exits.
    """

    def __init__(self) -> None:
        self._stack = contextlib.AsyncExitStack()
        self._workers: list[ManagedWorker[ClientT]] = []

    def __len__(self) -> int:
        return len(self._workers)

    def __iter__(self) -> Iterator[ManagedWorker[ClientT]]:
        return iter(self._workers)

    def __getitem__(self, index: int) -> ManagedWorker[ClientT]:
        return self._workers[index]

    def snapshot(self) -> tuple[ManagedWorker[ClientT], ...]:
        return tuple(self._workers)

    async def add(
        self,
        manager: Any,
        *,
        worker_id: str,
        client_factory: ClientFactory[ClientT] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ManagedWorker[ClientT]:
        channel = await self._stack.enter_async_context(manager)
        client = client_factory(channel) if client_factory is not None else channel
        worker = ManagedWorker(
            id=worker_id,
            channel=channel,
            client=client,
            metadata=metadata or {},
        )
        self._workers.append(worker)
        return worker

    async def __aenter__(self) -> WorkerPool[ClientT]:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._stack.aclose()


class WorkerHost:
    """Server-owned factory for managed worker pools.

    ``parent_services`` are exposed to workers over an in-band control stream
    on the same gRPC connection used for parent-to-worker calls.
    """

    def __init__(
        self,
        parent_services: Collection[IServable],
        *,
        tuning: TransportTuning = DEFAULT_TUNING,
    ) -> None:
        self.parent_services = tuple(parent_services)
        self.tuning = tuning

    def stdio_pool[PeerT: LogicalRpcPeer](
        self,
        argv: Sequence[str | Path],
        *,
        peer_factory: PeerFactory[PeerT],
        count: int = 1,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stderr: Any = None,
    ) -> StdioPeerPool[PeerT]:
        return StdioPeerPool(
            argv,
            peer_factory=peer_factory,
            size=count,
            tuning=self.tuning,
            cwd=cwd,
            env=env,
            stderr=stderr,
        ).with_parent_services(self.parent_services)

    @contextlib.asynccontextmanager
    async def stdio_channels[ClientT = Any](
        self,
        argv: Sequence[str | Path],
        *,
        client_factory: ClientFactory[ClientT] | None = None,
        count: int = 1,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stderr: Any = None,
    ) -> AsyncGenerator[WorkerPool[ClientT]]:
        if count <= 0:
            raise ValueError("count must be positive")

        async with WorkerPool[ClientT]() as pool:
            for index in range(count):
                if self.parent_services:
                    manager = stdio_worker_with_backchannel(
                        argv,
                        self.parent_services,
                        tuning=self.tuning,
                        cwd=cwd,
                        env=env,
                        stderr=stderr,
                    )
                else:
                    manager = stdio_worker(
                        argv,
                        tuning=self.tuning,
                        cwd=cwd,
                        env=env,
                        stderr=stderr,
                    )
                await pool.add(
                    manager,
                    worker_id=f"stdio-{index + 1}",
                    client_factory=client_factory,
                    metadata={"transport": "stdio", "index": index},
                )
            yield pool

    @contextlib.asynccontextmanager
    async def multiprocessing_channels[ClientT = Any](
        self,
        service_factory: ServiceFactory | BackchannelServiceFactory,
        *,
        client_factory: ClientFactory[ClientT] | None = None,
        count: int = 1,
        on_process_start: Callable[[Any], None] | None = None,
        preload: Sequence[str] = (),
        max_concurrency: int | None = None,
    ) -> AsyncGenerator[WorkerPool[ClientT]]:
        if count <= 0:
            raise ValueError("count must be positive")

        async with WorkerPool[ClientT]() as pool:
            for index in range(count):
                if self.parent_services:
                    manager = multiprocessing_worker_with_backchannel(
                        cast("BackchannelServiceFactory", service_factory),
                        self.parent_services,
                        on_process_start=on_process_start,
                        preload=preload,
                        tuning=self.tuning,
                        max_concurrency=max_concurrency,
                    )
                else:
                    if not callable(service_factory):
                        raise TypeError("service_factory must be callable")
                    manager = multiprocessing_worker(
                        cast("ServiceFactory", service_factory),
                        on_process_start=on_process_start,
                        preload=preload,
                        tuning=self.tuning,
                        max_concurrency=max_concurrency,
                    )
                await pool.add(
                    manager,
                    worker_id=f"multiprocessing-{index + 1}",
                    client_factory=client_factory,
                    metadata={"transport": "multiprocessing", "index": index},
                )
            yield pool

    @contextlib.asynccontextmanager
    async def inproc_channels[ClientT = Any](
        self,
        service_factory: InprocServiceFactory | InprocBackchannelServiceFactory,
        *,
        client_factory: ClientFactory[ClientT] | None = None,
        count: int = 1,
        max_concurrency: int | None = None,
    ) -> AsyncGenerator[WorkerPool[ClientT]]:
        """Yield local worker channels, using the multiprocessing-style API.

        Service factories execute in the current process, allowing tests to
        retain references to both worker and parent services.
        """
        if count <= 0:
            raise ValueError("count must be positive")

        async with WorkerPool[ClientT]() as pool:
            for index in range(count):
                if self.parent_services:
                    manager = inproc_worker_with_backchannel(
                        cast("InprocBackchannelServiceFactory", service_factory),
                        self.parent_services,
                        tuning=self.tuning,
                        max_concurrency=max_concurrency,
                    )
                else:
                    if not callable(service_factory):
                        raise TypeError("service_factory must be callable")
                    manager = inproc_worker(
                        cast("InprocServiceFactory", service_factory),
                        tuning=self.tuning,
                        max_concurrency=max_concurrency,
                    )
                await pool.add(
                    manager,
                    worker_id=f"inproc-{index + 1}",
                    client_factory=client_factory,
                    metadata={"transport": "inproc", "index": index},
                )
            yield pool
