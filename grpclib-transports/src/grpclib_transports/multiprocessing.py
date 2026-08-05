"""Multiprocessing pipe-pair helpers: forkserver contexts and dup'd FDs."""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import os
from collections.abc import AsyncGenerator, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any

# The submodule, explicitly: anyio's `__init__` does not import `to_thread`,
# so a plain `import anyio` would leave the attribute unbound.
import anyio.to_thread
from grpclib._typing import IServable
from grpclib.encoding.base import StatusDetailsCodecBase

from grpclib_transports.control import WorkerBackchannel, open_parent_control_peer
from grpclib_transports.pipes import PipeChannel, pipe_streams_from_fds
from grpclib_transports.protocol import DEFAULT_TUNING, TransportTuning, serve_h2

ServiceFactory = Callable[[], Collection[IServable]]
BackchannelServiceFactory = Callable[[WorkerBackchannel], Collection[IServable]]
_PROCESS_CLOSE_TIMEOUT = 3.0
# How long a worker gets to end itself after its channel closes, before the
# parent signals it. `_stop_process` gives the measurement and the reason.
_PROCESS_EXIT_GRACE = 2.0


def get_forkserver_context(
    *,
    preload: Sequence[str] = (),
) -> Any:
    """Return a ``multiprocessing`` forkserver context, optionally preloading modules."""
    context = mp.get_context("forkserver")
    if preload:
        context.set_forkserver_preload(list(preload))
    return context


@dataclass(frozen=True)
class MultiprocessingPipeEndpoint:
    """One end of a multiprocessing pipe pair.

    Call :meth:`open_channel` to create a :class:`~grpclib_transports.pipes.PipeChannel`
    backed by the pipe file descriptors.
    """

    read_connection: Any
    write_connection: Any
    transport_name: str = "multiprocessing"

    async def open_channel(
        self,
        *,
        tuning: TransportTuning = DEFAULT_TUNING,
        status_details_codec: StatusDetailsCodecBase | None = None,
    ) -> PipeChannel:
        read_fd = os.dup(self.read_connection.fileno())
        write_fd = os.dup(self.write_connection.fileno())
        reader, writer, transport = await pipe_streams_from_fds(
            read_fd,
            write_fd,
            transport_name=self.transport_name,
            tuning=tuning,
        )
        return PipeChannel(
            reader,
            writer,
            transport=transport,
            tuning=tuning,
            status_details_codec=status_details_codec,
        )

    def close_connections(self) -> None:
        self.read_connection.close()
        self.write_connection.close()


@dataclass(frozen=True)
class MultiprocessingPipePair:
    """A pair of :class:`MultiprocessingPipeEndpoint` — one for parent, one for child."""

    parent: MultiprocessingPipeEndpoint
    child: MultiprocessingPipeEndpoint
    context: Any

    def close_parent_connections(self) -> None:
        self.parent.close_connections()

    def close_child_connections(self) -> None:
        self.child.close_connections()


def multiprocessing_pipe_pair(
    *,
    context: Any | None = None,
    preload: Sequence[str] = (),
) -> MultiprocessingPipePair:
    """Create a :class:`MultiprocessingPipePair` for parent-child communication.

    If *context* is not given, calls :func:`get_forkserver_context` with *preload*.
    """
    ctx = context or get_forkserver_context(preload=preload)
    if context is not None and preload:
        ctx.set_forkserver_preload(list(preload))
    parent_read, child_write = ctx.Pipe(duplex=False)
    child_read, parent_write = ctx.Pipe(duplex=False)
    return MultiprocessingPipePair(
        parent=MultiprocessingPipeEndpoint(
            read_connection=parent_read,
            write_connection=parent_write,
        ),
        child=MultiprocessingPipeEndpoint(
            read_connection=child_read,
            write_connection=child_write,
        ),
        context=ctx,
    )


async def serve_multiprocessing_endpoint(
    endpoint: MultiprocessingPipeEndpoint,
    handlers: Collection[IServable],
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    """Serve gRPC over a multiprocessing pipe endpoint."""
    reader, _writer, transport = await pipe_streams_from_fds(
        os.dup(endpoint.read_connection.fileno()),
        os.dup(endpoint.write_connection.fileno()),
        transport_name=endpoint.transport_name,
        tuning=tuning,
    )
    endpoint.close_connections()
    await serve_h2(
        tuple(handlers),
        reader,
        transport,
        tuning=tuning,
        max_concurrency=max_concurrency,
        status_details_codec=status_details_codec,
    )


def _run_multiprocessing_worker(
    endpoint: MultiprocessingPipeEndpoint,
    service_factory: ServiceFactory,
    tuning: TransportTuning,
    max_concurrency: int | None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    async def run() -> None:
        await serve_multiprocessing_endpoint(
            endpoint,
            tuple(service_factory()),
            tuning=tuning,
            max_concurrency=max_concurrency,
            status_details_codec=status_details_codec,
        )

    asyncio.run(run())


def _run_multiprocessing_worker_with_backchannel(
    endpoint: MultiprocessingPipeEndpoint,
    service_factory: BackchannelServiceFactory,
    tuning: TransportTuning,
    max_concurrency: int | None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    async def run() -> None:
        backchannel = WorkerBackchannel()
        await serve_multiprocessing_endpoint(
            endpoint,
            (*service_factory(backchannel), backchannel.service()),
            tuning=tuning,
            max_concurrency=max_concurrency,
            status_details_codec=status_details_codec,
        )

    asyncio.run(run())


async def _stop_process(proc: Any) -> None:
    # The grace period runs first, and it is not politeness. A worker does its
    # own teardown after `serve_h2` returns, and the parent gets here about
    # 3 ms after it closes the channel. Nothing waited at all until this, so
    # `terminate()` reached a healthy worker while that teardown was still
    # running, and every worker died of SIGTERM with `exitcode` -15. A
    # nanopynix worker measured 51 ms to end itself, so the wait normally costs
    # that and no more.
    #
    # A caller that is already cancelled skips the wait, because the thread
    # hand-off raises at once, and it gets the old behaviour. That is the right
    # trade: a cancelled shutdown asks for speed.
    #
    # `proc.join`, and not a poll on `is_alive()`: a process exit is not an
    # event this loop can await, and the join returns the moment the child
    # ends. It is also the idiom the two calls below already use.
    await anyio.to_thread.run_sync(proc.join, _PROCESS_EXIT_GRACE)

    # `anyio.to_thread.run_sync`, not `asyncio.to_thread`: a pool shutdown
    # stops every worker at once, and asyncio spawns one unbounded thread per
    # call. anyio's shared CapacityLimiter puts a ceiling on that.
    if proc.is_alive():
        proc.terminate()
        await anyio.to_thread.run_sync(proc.join, _PROCESS_CLOSE_TIMEOUT)
    if proc.is_alive():
        proc.kill()
        await anyio.to_thread.run_sync(proc.join, _PROCESS_CLOSE_TIMEOUT)


@contextlib.asynccontextmanager
async def multiprocessing_worker(
    service_factory: ServiceFactory,
    *,
    context: Any | None = None,
    on_process_start: Callable[[Any], None] | None = None,
    preload: Sequence[str] = (),
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> AsyncGenerator[PipeChannel]:
    """Start a forkserver worker process and yield a gRPC channel to it.

    ``service_factory`` runs inside the worker process and must return the
    grpclib service handlers served by that worker.
    """
    pair = multiprocessing_pipe_pair(context=context, preload=preload)
    proc = pair.context.Process(
        target=_run_multiprocessing_worker,
        args=(pair.child, service_factory, tuning, max_concurrency, status_details_codec),
    )
    proc.start()
    if on_process_start is not None:
        on_process_start(proc)
    pair.close_child_connections()

    channel = await pair.parent.open_channel(
        tuning=tuning,
        status_details_codec=status_details_codec,
    )
    pair.close_parent_connections()
    try:
        yield channel
    finally:
        await channel.aclose()
        await _stop_process(proc)


@contextlib.asynccontextmanager
async def multiprocessing_worker_with_backchannel(
    service_factory: BackchannelServiceFactory,
    parent_services: Collection[IServable],
    *,
    context: Any | None = None,
    on_process_start: Callable[[Any], None] | None = None,
    preload: Sequence[str] = (),
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> AsyncGenerator[PipeChannel]:
    """Start a forkserver worker with an in-band parent-services backchannel.

    The yielded channel lets the parent call services hosted by the worker.
    ``parent_services`` are exposed to the worker over a long-lived
    bidirectional control stream on that same channel.
    """
    pair = multiprocessing_pipe_pair(context=context, preload=preload)
    proc = pair.context.Process(
        target=_run_multiprocessing_worker_with_backchannel,
        args=(pair.child, service_factory, tuning, max_concurrency, status_details_codec),
    )
    proc.start()
    if on_process_start is not None:
        on_process_start(proc)
    pair.close_child_connections()

    channel = await pair.parent.open_channel(
        tuning=tuning,
        status_details_codec=status_details_codec,
    )
    pair.close_parent_connections()
    try:
        async with open_parent_control_peer(channel, parent_services):
            yield channel
    finally:
        await channel.aclose()
        await _stop_process(proc)
