"""In-process pipe-pair helpers for testing gRPC services over real H2."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator, Callable, Collection
from dataclasses import dataclass

from grpclib._typing import IServable
from grpclib.encoding.base import StatusDetailsCodecBase

from grpclib_transports.control import WorkerBackchannel, open_parent_control_peer
from grpclib_transports.pipes import PipeChannel, pipe_streams_from_fds
from grpclib_transports.protocol import DEFAULT_TUNING, TransportTuning, serve_h2

ServiceFactory = Callable[[], Collection[IServable]]
BackchannelServiceFactory = Callable[[WorkerBackchannel], Collection[IServable]]


@dataclass(frozen=True)
class InprocPipeEndpoint:
    """One end of an in-process pipe pair."""

    read_fd: int
    write_fd: int
    transport_name: str = "inproc"

    async def open_channel(
        self,
        *,
        tuning: TransportTuning = DEFAULT_TUNING,
        status_details_codec: StatusDetailsCodecBase | None = None,
    ) -> PipeChannel:
        """Open a gRPC channel backed by this endpoint's pipe descriptors."""
        reader, writer, transport = await pipe_streams_from_fds(
            os.dup(self.read_fd),
            os.dup(self.write_fd),
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
        """Close the endpoint's original pipe descriptors."""
        for fd in (self.read_fd, self.write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


@dataclass(frozen=True)
class InprocPipePair:
    """A pair of :class:`InprocPipeEndpoint` objects for local communication."""

    parent: InprocPipeEndpoint
    child: InprocPipeEndpoint

    def close_parent_connections(self) -> None:
        """Close the parent's original pipe descriptors."""
        self.parent.close_connections()

    def close_child_connections(self) -> None:
        """Close the child's original pipe descriptors."""
        self.child.close_connections()


def inproc_pipe_pair() -> InprocPipePair:
    """Create an :class:`InprocPipePair` for two local H2 endpoints."""
    parent_read, child_write = os.pipe()
    child_read, parent_write = os.pipe()
    return InprocPipePair(
        parent=InprocPipeEndpoint(parent_read, parent_write),
        child=InprocPipeEndpoint(child_read, child_write),
    )


async def serve_inproc_endpoint(
    endpoint: InprocPipeEndpoint,
    handlers: Collection[IServable],
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    """Serve gRPC over an in-process pipe endpoint."""
    reader, _writer, transport = await pipe_streams_from_fds(
        os.dup(endpoint.read_fd),
        os.dup(endpoint.write_fd),
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


@contextlib.asynccontextmanager
async def inproc_worker(
    service_factory: ServiceFactory,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> AsyncGenerator[PipeChannel]:
    """Serve local worker services and yield a channel to them.

    Unlike :func:`multiprocessing_worker`, the service factory runs in this
    process. This is intended for tests that need references to both sides.
    """
    handlers = tuple(service_factory())
    pair = inproc_pipe_pair()
    server_task = asyncio.create_task(
        serve_inproc_endpoint(
            pair.child,
            handlers,
            tuning=tuning,
            max_concurrency=max_concurrency,
            status_details_codec=status_details_codec,
        ),
        name="inproc-worker-server",
    )
    try:
        channel = await pair.parent.open_channel(
            tuning=tuning,
            status_details_codec=status_details_codec,
        )
    except BaseException:
        pair.close_parent_connections()
        pair.close_child_connections()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        raise
    pair.close_parent_connections()
    try:
        yield channel
    finally:
        await channel.aclose()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        pair.close_child_connections()


@contextlib.asynccontextmanager
async def inproc_worker_with_backchannel(
    service_factory: BackchannelServiceFactory,
    parent_services: Collection[IServable],
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> AsyncGenerator[PipeChannel]:
    """Serve local worker services with an in-band parent-services backchannel."""
    backchannel = WorkerBackchannel()

    async with (
        inproc_worker(
            lambda: (*service_factory(backchannel), backchannel.service()),
            tuning=tuning,
            max_concurrency=max_concurrency,
            status_details_codec=status_details_codec,
        ) as channel,
        open_parent_control_peer(channel, parent_services),
    ):
        yield channel
