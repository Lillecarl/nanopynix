"""Stdio transport: H2 over a subprocess's stdin/stdout pipe pair."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import sys
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, BinaryIO

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
# How long a worker gets to end itself after its channel closes, before the
# parent signals it. `_close_worker_process` gives the measurement and the
# reason; `multiprocessing._stop_process` carries the same number.
_PROCESS_EXIT_GRACE = 2.0


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


def take_wire_descriptors() -> tuple[BinaryIO, BinaryIO]:
    """Move the H2 pipe pair off descriptors 0 and 1, and return the two files.

    **Descriptor 1 carries the wire, and a serving process does not own it.**
    A redirection of ``sys.stdout`` is a Python-level rebinding, so it cannot
    stop a C++ library, a C extension or a subprocess from writing to the
    descriptor itself. Every such byte becomes an HTTP/2 frame, and the peer
    reports a protocol error that names nothing about where the byte came
    from.

    Descriptor 0 has the matching problem in the other direction: a subprocess
    that reads it takes frames the transport needed. ``ssh`` asking for a
    passphrase is the case that reaches a real deployment.

    So this takes both descriptors for the transport, and leaves behind what a
    program that still uses them expects: descriptor 1 becomes a duplicate of
    descriptor 2, so a stray write is a log line rather than a corruption, and
    descriptor 0 becomes ``/dev/null``, so a stray read gets end of file.

    Call it once, before anything writes. It is the first act of
    :func:`stdio_streams`, and a process serves stdio once.
    """
    # First: whatever is already in the buffer of `sys.stdout` was written
    # before this function existed, and belongs to the descriptor it was
    # written for. Flushing after the exchange below would send it to stderr.
    with contextlib.suppress(ValueError, OSError):
        sys.stdout.flush()

    wire_write_fd = os.dup(1)
    try:
        os.dup2(2, 1)
    except OSError:
        # A process with no stderr. `/dev/null` still keeps a stray write off
        # the wire, which is the whole subject here.
        _point_descriptor_at_devnull(1, os.O_WRONLY)

    wire_read_fd = os.dup(0)
    _point_descriptor_at_devnull(0, os.O_RDONLY)

    return os.fdopen(wire_read_fd, "rb", buffering=0), os.fdopen(wire_write_fd, "wb", buffering=0)


def _point_descriptor_at_devnull(fd: int, flags: int) -> None:
    devnull = os.open(os.devnull, flags)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


async def stdio_streams(
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, StdioTransport]:
    wire_read, wire_write = take_wire_descriptors()

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        wire_read,
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
        wire_write,
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

    :func:`take_wire_descriptors` moves the pipe pair off descriptors 0 and 1
    first, so that only H2 frames go over the wire. The redirection below adds
    the one case that cannot reach: a caller that replaced ``sys.stdout`` with
    an object of its own, which writes wherever that object writes.
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
    """Wait for the worker to end itself, then make sure that it has.

    **The grace period runs first, and it is not politeness.** A worker does
    its own teardown after ``serve_h2`` returns, and the parent gets here
    milliseconds after it closes the channel. Nothing waited at all until
    this, so ``terminate()`` reached a healthy worker while that teardown was
    still running, and every worker died of SIGTERM with a return code of -15.
    A nanopynix worker measured 51 ms to end itself, so the wait normally
    costs that and no more.

    ``multiprocessing._stop_process`` carries the same period, for the same
    reason. A caller that is already cancelled skips it, because the wait
    raises at once, and gets the old behaviour -- which is the right trade,
    because a cancelled shutdown asks for speed.
    """
    if proc.returncode is not None:
        return

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), _PROCESS_EXIT_GRACE)
        return

    # ProcessLookupError: the worker ended between the wait above and this
    # line, and `send_signal` refuses a process it has already reaped. The
    # grace period made that window wide enough to reach.
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), _SUBPROCESS_CLOSE_TIMEOUT)
        return

    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
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
    on_process_start: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> AsyncGenerator[StdioChannel]:
    """Spawn a subprocess and yield a :class:`StdioChannel` connected to its stdin/stdout.

    Use as an async context manager.  The subprocess is terminated on exit.

    ``on_process_start`` receives the process as soon as it exists, and is the
    only way a caller reaches it: the channel yielded below carries the wire
    and nothing about the peer. ``multiprocessing_worker`` takes the same hook
    for the same reason. A caller needs it to read the exit status, which is
    what tells an abort from an ordinary exit, and to act on the pid.
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
    # Before the guard below, so that a caller which acts on the pid gets to
    # do it for a process that failed to give pipes as well.
    if on_process_start is not None:
        on_process_start(proc)
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
    on_process_start: Callable[[asyncio.subprocess.Process], None] | None = None,
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
            on_process_start=on_process_start,
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
