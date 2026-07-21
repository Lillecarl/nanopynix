"""Python Unix-listener supervision for isolated native Nix connections."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
from anyio.abc import SocketAttribute

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

    from anyio.abc import Process, SocketListener, SocketStream, TaskGroup

    from nanopynix.rpc.daemon._config import DaemonConfig

logger = logging.getLogger(__name__)


def _write_all(fd: int, data: bytes) -> None:
    """Write the small process-private configuration without blocking the loop."""
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _terminate_process_group(process: Process) -> None:
    """Terminate a connection worker together with any Nix descendants it owns."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        # The connection may have exited between returncode inspection and the
        # group signal; its waiter will observe the completed process.
        return


class DaemonSupervisor:
    """Own a Unix listener and isolate every daemon client in a fresh process.

    This role never initialises Nix. That makes process creation safe even
    after its asyncio runtime is active, and prevents Nix's process-global
    daemon logger from crossing connection boundaries.

    ``start()`` and ``close()`` are typically invoked as separate gRPC
    control-plane calls, which land on different asyncio tasks. anyio's
    ``CancelScope``/``TaskGroup`` must be entered and exited by the same
    task, so the task group cannot be opened in ``start()`` and closed in
    ``close()`` directly. Instead its entire lifetime lives inside one
    dedicated background task (``_run``); ``start()``/``close()`` merely
    signal that task via ``anyio.Event``, which -- unlike ``CancelScope``/
    ``TaskGroup`` -- has no task affinity and is safe to set/wait from any
    task.
    """

    def __init__(self, socket_path: Path, config: DaemonConfig) -> None:
        self._socket_path = socket_path
        self._config = config
        self._listener: SocketListener | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._accept_scope: anyio.CancelScope | None = None
        self._stop_event: anyio.Event | None = None
        self._children: set[Process] = set()
        self._spawn_lock = anyio.Lock()
        self._closing = False

    @property
    def socket_path(self) -> Path:
        """Filesystem path of the owned Unix listener."""
        return self._socket_path

    @property
    def running(self) -> bool:
        """Whether this supervisor still owns an active listener."""
        return self._listener is not None and not self._closing

    @property
    def active_connections(self) -> int:
        """Number of connection worker processes not yet reaped."""
        return len(self._children)

    async def __aenter__(self) -> DaemonSupervisor:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Bind the listener and start accepting native Nix connections."""
        if self._listener is not None:
            raise RuntimeError("daemon supervisor is already running")
        if self._closing:
            raise RuntimeError("closed daemon supervisor cannot be restarted")
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            raise FileExistsError(self._socket_path)
        try:
            listener = await anyio.create_unix_listener(self._socket_path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                self._socket_path.unlink()
            raise
        self._listener = listener
        self._stop_event = anyio.Event()
        ready = anyio.Event()
        self._run_task = asyncio.create_task(
            self._run(listener, ready), name=f"nanopynix-daemon-supervisor:{self._socket_path}"
        )
        await ready.wait()

    async def close(self) -> None:
        """Stop accepting and terminate every owned connection worker."""
        if self._closing:
            return
        self._closing = True
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        run_task = self._run_task
        self._run_task = None
        if run_task is not None:
            await run_task

        listener = self._listener
        self._listener = None
        if listener is not None:
            await listener.aclose()
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    async def _run(self, listener: SocketListener, ready: anyio.Event) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            raise RuntimeError("daemon supervisor is not running")
        async with anyio.create_task_group() as tg:
            self._accept_scope = anyio.CancelScope()
            tg.start_soon(self._run_accept_loop, listener, tg)
            ready.set()
            await stop_event.wait()
            # Cancel the accept loop rather than closing the listener first:
            # the cancellation is cleanly swallowed by accept_scope itself
            # (a self-cancellation), whereas closing the listener out from
            # under a pending accept() would raise ClosedResourceError,
            # which -- unlike a self-cancellation -- would propagate out of
            # the task group and get wrapped in a BaseExceptionGroup. The
            # listener is closed by close() only after this task group has
            # fully exited.
            self._accept_scope.cancel()

            async with self._spawn_lock:
                children = tuple(self._children)
            for child in children:
                _terminate_process_group(child)
            # Exiting `async with tg` here is not cancelled: in-flight
            # connection handlers are left to notice their (now-terminated)
            # child process exit and finish on their own, mirroring the
            # previous gather(..., return_exceptions=True) join instead of
            # hard-cancelling them mid-cleanup.

    async def _run_accept_loop(self, listener: SocketListener, task_group: TaskGroup) -> None:
        accept_scope = self._accept_scope
        if accept_scope is None:
            raise RuntimeError("daemon supervisor is not running")
        with accept_scope:
            await listener.serve(self._serve_connection, task_group)

    async def _serve_connection(self, connection: SocketStream) -> None:
        raw = connection.extra(SocketAttribute.raw_socket)
        # anyio accepts from a non-blocking listener. Nix's FdSource uses
        # blocking reads, so restore the daemon protocol's expected
        # descriptor mode before the exec'd worker inherits it.
        raw.setblocking(True)
        child: Process | None = None
        try:
            async with self._spawn_lock:
                if self._closing:
                    return
                config_read_fd, config_write_fd = os.pipe()
                try:
                    try:
                        child = await anyio.open_process(
                            [
                                sys.executable,
                                "-m",
                                "nanopynix.rpc.daemon._connection",
                                "--connection-fd",
                                str(raw.fileno()),
                                "--config-fd",
                                str(config_read_fd),
                            ],
                            stdin=None,
                            stdout=None,
                            stderr=None,
                            pass_fds=(raw.fileno(), config_read_fd),
                            # A connection can launch Nix build descendants.
                            # Its dedicated group lets supervisor shutdown
                            # terminate only that connection tree.
                            start_new_session=True,
                        )
                    except BaseException:
                        os.close(config_write_fd)
                        raise
                finally:
                    os.close(config_read_fd)
                self._children.add(child)
                try:
                    await anyio.to_thread.run_sync(_write_all, config_write_fd, self._config.to_bytes())
                except BaseException:
                    _terminate_process_group(child)
                    await child.wait()
                    raise
            await connection.aclose()
            returncode = await child.wait()
            if returncode != 0:
                logger.warning("daemon connection worker exited with status %d", returncode)
        finally:
            await connection.aclose()
            if child is not None:
                self._children.discard(child)
