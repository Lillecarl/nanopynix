"""SSH transport: H2 over asyncssh sessions and channels."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grpclib import client

from grpclib_transports.protocol import (
    DEFAULT_TUNING,
    BaseCustomTransport,
    TransportTuning,
    init_h2_transport,
    make_config,
    pause_h2_protocol,
    pump,
    resume_h2_protocol,
    serve_h2,
    signal_stop,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from grpclib._typing import IServable
    from grpclib.encoding.base import StatusDetailsCodecBase
    from grpclib.protocol import H2Protocol

_ASYNCSSH_AVAILABLE = importlib.util.find_spec("asyncssh") is not None


def is_ssh_available() -> bool:
    """Return ``True`` if ``asyncssh`` is importable."""
    return _ASYNCSSH_AVAILABLE


def _load_asyncssh() -> Any:
    if not _ASYNCSSH_AVAILABLE:
        raise ImportError(
            "grpclib_transports.ssh requires asyncssh. "
            "Install the Nix package with asyncssh enabled to use SSH transports."
        )
    # Deferred on purpose. `asyncssh` is an optional dependency (the `ssh`
    # extra), so a top-level import would make this module -- and therefore
    # the package's `__init__` -- unimportable wherever the extra is absent,
    # which is every nanopynix environment: nanopynix uses the stdio and
    # multiprocessing transports only.
    import asyncssh  # noqa: PLC0415 -- optional dependency, see above

    return asyncssh


def _required_asyncssh_attr(obj: Any, name: str) -> Any:
    value = getattr(obj, name, None)
    if value is None:
        raise RuntimeError(f"AsyncSSH writer is missing required {name!r} attribute")
    return value


@dataclass(frozen=True)
class _AsyncSshWriterAdapter:
    writer: Any
    channel: Any
    session: Any

    @classmethod
    def from_writer(cls, writer: Any) -> _AsyncSshWriterAdapter:
        return cls(
            writer=writer,
            channel=_required_asyncssh_attr(writer, "_chan"),
            session=_required_asyncssh_attr(writer, "_session"),
        )


class SshTransport(BaseCustomTransport):
    """An asyncio transport that wraps an asyncssh channel.

    Forwards asyncssh session ``pause_writing``/``resume_writing`` callbacks
    to the H2 protocol for end-to-end backpressure.  Restores the original
    session callbacks on close or abort.
    """

    def __init__(
        self,
        reader: Any,
        writer: Any,
        *,
        tuning: TransportTuning = DEFAULT_TUNING,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._tuning = tuning
        self._adapter = _AsyncSshWriterAdapter.from_writer(writer)

        chan = self._adapter.channel
        chan.set_write_buffer_limits(
            high=tuning.write_high_water,
            low=tuning.write_low_water,
        )
        self._chan = chan

        self._session = self._adapter.session
        self._orig_pause_writing = self._session.pause_writing
        self._orig_resume_writing = self._session.resume_writing
        self._session.pause_writing = self._on_pause_writing
        self._session.resume_writing = self._on_resume_writing

    def _restore_session_callbacks(self) -> None:
        if self._orig_pause_writing is not None:
            self._session.pause_writing = self._orig_pause_writing
            self._orig_pause_writing = None
        if self._orig_resume_writing is not None:
            self._session.resume_writing = self._orig_resume_writing
            self._orig_resume_writing = None

    def _on_pause_writing(self) -> None:
        if self._orig_pause_writing is not None:
            self._orig_pause_writing()
        pause_h2_protocol(self._protocol)

    def _on_resume_writing(self) -> None:
        if self._orig_resume_writing is not None:
            self._orig_resume_writing()
        resume_h2_protocol(self._protocol)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self._writer.write(data)

    def get_write_buffer_size(self) -> int:
        return self._chan.get_write_buffer_size()

    def close(self) -> None:
        self._closing = True
        self._restore_session_callbacks()
        self._writer.close()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name in ("username", "session", "channel"):
            return self._writer.get_extra_info(name)
        return self._chan.get_extra_info(name, default)

    def abort(self) -> None:
        self._closing = True
        self._restore_session_callbacks()
        self._writer.abort()

    def can_write_eof(self) -> bool:
        return self._chan.can_write_eof()

    def write_eof(self) -> None:
        self._writer.write_eof()

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        self._chan.set_write_buffer_limits(high=high, low=low)


async def serve_ssh(
    handlers: list[IServable],
    host: str = "127.0.0.1",
    port: int = 8022,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    """Start an asyncssh server that speaks H2 on each session.

    Generates an ephemeral Ed25519 key and accepts connections indefinitely.
    Shuts down gracefully on SIGINT or SIGTERM.
    """
    asyncssh = _load_asyncssh()

    key = asyncssh.generate_private_key("ssh-ed25519")

    class _DemoSSHServer(asyncssh.SSHServer):
        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return True

    async def session_handler(stdin: Any, stdout: Any, _stderr: Any) -> None:
        transport = SshTransport(stdin, stdout, tuning=tuning)
        await serve_h2(
            handlers,
            stdin,
            transport,
            tuning=tuning,
            max_concurrency=max_concurrency,
            status_details_codec=status_details_codec,
        )

    acceptor = await asyncssh.create_server(
        _DemoSSHServer,
        host,
        port,
        server_host_keys=[key],
        session_factory=session_handler,
        encoding=None,
        line_editor=False,
    )

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: signal_stop(stop))
    try:
        await stop
    finally:
        acceptor.close()
        await acceptor.wait_closed()


class SshChannel(client.Channel):
    """A gRPC channel that speaks H2 over an asyncssh session."""

    def __init__(
        self,
        reader: Any,
        writer: Any,
        *,
        tuning: TransportTuning = DEFAULT_TUNING,
        **kwargs: Any,
    ) -> None:
        config = kwargs.pop("config", make_config(tuning))
        super().__init__(host="ssh", port=0, config=config, **kwargs)  # pyright: ignore[reportUnknownMemberType] -- grpclib Channel ssl param has Unknown in type stubs
        self._ssh_reader = reader
        self._ssh_writer = writer
        self._tuning = tuning
        self._pump_task: asyncio.Task[None] | None = None
        self._ssh_transport: SshTransport | None = None

    async def _create_connection(self) -> H2Protocol:
        protocol = self._protocol_factory()
        transport = SshTransport(
            self._ssh_reader,
            self._ssh_writer,
            tuning=self._tuning,
        )
        self._ssh_transport = transport
        init_h2_transport(protocol, transport, tuning=self._tuning)
        self._pump_task = asyncio.create_task(
            pump(protocol, self._ssh_reader, tuning=self._tuning),
            name="ssh-pump",
        )
        return protocol

    def close(self) -> None:
        super().close()
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        if self._ssh_transport is not None:
            self._ssh_transport.close()
        self._ssh_writer.close()

    async def aclose(self) -> None:
        self.close()
        if self._pump_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task

        wait_closed = getattr(self._ssh_writer, "wait_closed", None)
        if wait_closed is not None:
            with contextlib.suppress(ConnectionError, OSError):
                await wait_closed()


@contextlib.asynccontextmanager
async def connect_ssh(
    host: str,
    port: int = 22,
    *,
    username: str | None = None,
    password: str | None = None,
    known_hosts: Any = None,
    tuning: TransportTuning = DEFAULT_TUNING,
    status_details_codec: StatusDetailsCodecBase | None = None,
    **kwargs: Any,
) -> AsyncGenerator[SshChannel]:
    """Connect to an SSH server and yield an :class:`SshChannel`.

    Use as an async context manager.  The SSH session and channel are closed
    on exit.
    """
    asyncssh = _load_asyncssh()
    async with asyncssh.connect(
        host,
        port,
        username=username,
        password=password,
        known_hosts=known_hosts,
        **kwargs,
    ) as conn:
        stdin, stdout, _stderr = await conn.open_session(encoding=None)
        channel = SshChannel(
            stdout,
            stdin,
            tuning=tuning,
            status_details_codec=status_details_codec,
        )
        try:
            yield channel
        finally:
            await channel.aclose()


@contextlib.asynccontextmanager
async def connect_ssh_stdio(
    host: str,
    command: str,
    port: int = 22,
    *,
    username: str | None = None,
    password: str | None = None,
    known_hosts: Any = None,
    tuning: TransportTuning = DEFAULT_TUNING,
    status_details_codec: StatusDetailsCodecBase | None = None,
    **kwargs: Any,
) -> AsyncGenerator[SshChannel]:
    """Execute *command* over SSH and speak gRPC over its stdin/stdout.

    This is the OpenSSH-compatible deployment mode: the remote command is a
    stdio gRPC worker process, and SSH only provides the encrypted byte stream.
    """
    asyncssh = _load_asyncssh()
    async with asyncssh.connect(
        host,
        port,
        username=username,
        password=password,
        known_hosts=known_hosts,
        **kwargs,
    ) as conn:
        proc = await conn.create_process(command, encoding=None)
        channel = SshChannel(
            proc.stdout,
            proc.stdin,
            tuning=tuning,
            status_details_codec=status_details_codec,
        )
        try:
            yield channel
        finally:
            await channel.aclose()
            proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
