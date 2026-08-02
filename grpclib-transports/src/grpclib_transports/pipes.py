"""Raw OS pipe transport: H2 over arbitrary binary file descriptors.

More general than stdio — accepts any :class:`~io.BinaryIO` pair rather
than being hardwired to ``sys.stdin``/``sys.stdout``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Any, BinaryIO

from grpclib import client

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
)

if TYPE_CHECKING:
    from grpclib.protocol import H2Protocol


class PipeTransport(BaseCustomTransport):
    """An asyncio transport that wraps a raw OS pipe pair.

    Differs from :class:`~grpclib_transports.stdio.StdioTransport` in that
    it accepts arbitrary binary file objects rather than being hardwired to
    ``sys.stdin``/``sys.stdout``.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        transport_name: str = "pipe",
        tuning: TransportTuning = DEFAULT_TUNING,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._transport_name = transport_name
        writer.transport.set_write_buffer_limits(
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
            return local_process_identity(transport=self._transport_name)
        return self._writer.get_extra_info(name, default)

    def abort(self) -> None:
        self._closing = True
        self._writer.close()

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:
        pass


async def pipe_streams(
    read_pipe: BinaryIO,
    write_pipe: BinaryIO,
    *,
    transport_name: str = "pipe",
    tuning: TransportTuning = DEFAULT_TUNING,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, PipeTransport]:
    """Create a reader/writer/transport triple from two binary file objects."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        read_pipe,
    )

    transport_ref: list[PipeTransport] = []

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
        write_pipe,
    )
    writer = asyncio.StreamWriter(t, proto, reader, loop)
    transport = PipeTransport(
        reader,
        writer,
        transport_name=transport_name,
        tuning=tuning,
    )
    transport_ref.append(transport)
    return reader, writer, transport


async def pipe_streams_from_fds(
    read_fd: int,
    write_fd: int,
    *,
    transport_name: str = "pipe",
    tuning: TransportTuning = DEFAULT_TUNING,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, PipeTransport]:
    """Like :func:`pipe_streams` but takes integer file descriptors."""
    read_pipe = os.fdopen(read_fd, "rb", buffering=0)
    write_pipe = os.fdopen(write_fd, "wb", buffering=0)
    return await pipe_streams(
        read_pipe,
        write_pipe,
        transport_name=transport_name,
        tuning=tuning,
    )


class PipeChannel(client.Channel):
    """A gRPC channel that speaks H2 over a raw OS pipe pair."""

    def __init__(
        self,
        reader: Any,
        writer: Any,
        *,
        transport: PipeTransport | None = None,
        tuning: TransportTuning = DEFAULT_TUNING,
        **kwargs: Any,
    ) -> None:
        config = kwargs.pop("config", make_config(tuning))
        super().__init__(host="pipe", port=0, config=config, **kwargs)  # pyright: ignore[reportUnknownMemberType] -- grpclib Channel ssl param has Unknown in type stubs
        self._pipe_reader = reader
        self._pipe_writer = writer
        self._pipe_transport = transport
        self._tuning = tuning
        self._pump_task: asyncio.Task[None] | None = None

    async def _create_connection(self) -> H2Protocol:
        protocol = self._protocol_factory()
        transport = self._pipe_transport or PipeTransport(
            self._pipe_reader,
            self._pipe_writer,
            tuning=self._tuning,
        )
        self._pipe_transport = transport
        init_h2_transport(protocol, transport, tuning=self._tuning)
        self._pump_task = asyncio.create_task(
            pump(protocol, self._pipe_reader, tuning=self._tuning),
            name="pipe-pump",
        )
        return protocol

    def close(self) -> None:
        super().close()
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        if self._pipe_transport is not None:
            self._pipe_transport.close()

    async def aclose(self) -> None:
        self.close()
        if self._pump_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
        with contextlib.suppress(ConnectionError, OSError):
            await self._pipe_writer.wait_closed()
