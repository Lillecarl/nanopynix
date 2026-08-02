"""Stdio transport: H2 over a subprocess's stdin/stdout pipe pair."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import sys
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any

from grpclib import client
from grpclib._typing import IServable
from grpclib.encoding.base import StatusDetailsCodecBase

from grpclib_transports.control import WorkerBackchannel, open_parent_control_peer
from grpclib_transports.protocol import (
    DEFAULT_TUNING,
    BaseCustomTransport,
    TransportTuning,
    init_h2_transport,
    local_process_identity,
    make_config,
    pause_h2_protocol,
    pump,
    resume_h2_protocol,
    serve_h2,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from pathlib import Path

    from grpclib.protocol import H2Protocol

BackchannelServiceFactory = Callable[[WorkerBackchannel], Collection[IServable]]
_SUBPROCESS_CLOSE_TIMEOUT = 5.0


class StdioTransport(BaseCustomTransport):
    """An asyncio transport that wraps a stdio subprocess pipe pair."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        tuning: TransportTuning = DEFAULT_TUNING,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._tuning = tuning

        pipe_transport = writer.transport
        pipe_transport.set_write_buffer_limits(
            high=tuning.write_high_water,
            low=tuning.write_low_water,
        )

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self._writer.write(data)

    def get_write_buffer_size(self) -> int:
        return self._writer.transport.get_write_buffer_size()

    def close(self) -> None:
        self._closing = True
        self._writer.close()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "peer_identity":
            return local_process_identity(transport="stdio")
        return self._writer.get_extra_info(name, default)

    def abort(self) -> None:
        # Pipes have no hard abort — close() is the best we can do.
        self._closing = True
        self._writer.close()

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:
        pass


async def stdio_streams(
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, StdioTransport]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        sys.stdin.buffer,
    )

    transport_ref: list[StdioTransport] = []

    class _Bridge(asyncio.Protocol):
        def __init__(self) -> None:
            self._closed = loop.create_future()

        def pause_writing(self) -> None:
            if transport_ref:
                pause_h2_protocol(transport_ref[0].get_protocol())

        def resume_writing(self) -> None:
            if transport_ref:
                resume_h2_protocol(transport_ref[0].get_protocol())

        def connection_lost(self, exc: Exception | None) -> None:
            if self._closed.done():
                return
            if exc is None:
                self._closed.set_result(None)
            else:
                self._closed.set_exception(exc)

        def _get_close_waiter(self, _stream: Any) -> asyncio.Future[None]:
            return self._closed

    t, proto = await loop.connect_write_pipe(
        _Bridge,
        sys.stdout.buffer,
    )
    writer = asyncio.StreamWriter(t, proto, reader, loop)

    transport = StdioTransport(reader, writer, tuning=tuning)
    transport_ref.append(transport)

    return reader, writer, transport


async def serve_stdio(
    handlers: list[IServable],
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    """Serve gRPC over the current process's stdin/stdout.

    Redirects stdout to stderr before starting so that only H2 frames go
    over the pipe.
    """
    reader, _writer, transport = await stdio_streams(tuning=tuning)

    with contextlib.redirect_stdout(sys.stderr):
        await serve_h2(
            handlers,
            reader,
            transport,
            tuning=tuning,
            max_concurrency=max_concurrency,
            status_details_codec=status_details_codec,
        )


async def serve_stdio_with_backchannel(
    service_factory: BackchannelServiceFactory,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    """Serve stdio gRPC handlers plus a worker-to-parent backchannel service."""
    backchannel = WorkerBackchannel()
    await serve_stdio(
        [*service_factory(backchannel), backchannel.service()],
        tuning=tuning,
        max_concurrency=max_concurrency,
        status_details_codec=status_details_codec,
    )


def bump_subprocess_pipe_buffers(
    proc: asyncio.subprocess.Process,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
) -> None:
    popen = getattr(getattr(proc, "_transport", None), "_proc", None)
    if popen is None:
        return
    for attr in ("stdin", "stdout", "stderr"):
        f = getattr(popen, attr, None)
        if f is not None:
            with contextlib.suppress(OSError):
                fcntl.fcntl(f.fileno(), fcntl.F_SETPIPE_SZ, tuning.buffer_size)


async def _close_worker_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    proc.terminate()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), _SUBPROCESS_CLOSE_TIMEOUT)
        return

    if proc.returncode is None:
        proc.kill()
        await proc.wait()


@contextlib.asynccontextmanager
async def stdio_worker(
    argv: Sequence[str | Path],
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stderr: Any = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> AsyncGenerator[StdioChannel]:
    """Spawn a subprocess and yield a :class:`StdioChannel` connected to its stdin/stdout.

    Use as an async context manager.  The subprocess is terminated on exit.
    """
    if not argv:
        raise ValueError("argv must not be empty")

    proc = await asyncio.create_subprocess_exec(
        *(str(arg) for arg in argv),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )
    if proc.stdin is None or proc.stdout is None:
        await _close_worker_process(proc)
        raise RuntimeError("stdio worker was not started with stdin/stdout pipes")

    bump_subprocess_pipe_buffers(proc, tuning=tuning)
    channel = StdioChannel(
        proc.stdout,
        proc.stdin,
        tuning=tuning,
        status_details_codec=status_details_codec,
    )
    try:
        yield channel
    finally:
        await channel.aclose()
        await _close_worker_process(proc)


@contextlib.asynccontextmanager
async def stdio_worker_with_backchannel(
    argv: Sequence[str | Path],
    parent_services: Collection[IServable],
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stderr: Any = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> AsyncGenerator[StdioChannel]:
    """Spawn a stdio worker and expose parent services on the same channel."""
    async with (
        stdio_worker(
            argv,
            tuning=tuning,
            cwd=cwd,
            env=env,
            stderr=stderr,
            status_details_codec=status_details_codec,
        ) as channel,
        open_parent_control_peer(channel, parent_services),
    ):
        yield channel


class StdioChannel(client.Channel):
    """A gRPC channel that speaks H2 over a subprocess stdio pipe pair."""

    def __init__(
        self,
        reader: Any,
        writer: Any,
        *,
        transport: StdioTransport | None = None,
        tuning: TransportTuning = DEFAULT_TUNING,
        **kwargs: Any,
    ) -> None:
        config = kwargs.pop("config", make_config(tuning))
        super().__init__(host="stdio", port=0, config=config, **kwargs)  # pyright: ignore[reportUnknownMemberType] -- grpclib Channel ssl param has Unknown in type stubs
        self._stdio_reader = reader
        self._stdio_writer = writer
        self._stdio_transport = transport
        self._tuning = tuning
        self._pump_task: asyncio.Task[None] | None = None

    async def _create_connection(self) -> H2Protocol:
        protocol = self._protocol_factory()
        transport = self._stdio_transport or StdioTransport(self._stdio_reader, self._stdio_writer, tuning=self._tuning)
        self._stdio_transport = transport
        init_h2_transport(protocol, transport, tuning=self._tuning)
        self._pump_task = asyncio.create_task(
            pump(protocol, self._stdio_reader, tuning=self._tuning),
            name="stdio-pump",
        )
        return protocol

    def close(self) -> None:
        super().close()
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        if self._stdio_transport is not None:
            self._stdio_transport.close()

    async def aclose(self) -> None:
        self.close()
        if self._pump_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
        with contextlib.suppress(ConnectionError, OSError):
            await self._stdio_writer.wait_closed()
