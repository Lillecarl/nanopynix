"""Python Unix-listener supervision for isolated native Nix connections."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

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


def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
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
    """

    def __init__(self, socket_path: Path, config: DaemonConfig) -> None:
        self._socket_path = socket_path
        self._config = config
        self._listener: socket.socket | None = None
        self._accept_task: asyncio.Task[None] | None = None
        self._connection_tasks: set[asyncio.Task[None]] = set()
        self._children: set[asyncio.subprocess.Process] = set()
        self._spawn_lock = asyncio.Lock()
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
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self._socket_path))
            listener.listen()
            listener.setblocking(False)
        except BaseException:
            listener.close()
            with contextlib.suppress(FileNotFoundError):
                self._socket_path.unlink()
            raise
        self._listener = listener
        self._accept_task = asyncio.create_task(self._accept_loop(), name="nanopynix-daemon-accept")

    async def close(self) -> None:
        """Stop accepting and terminate every owned connection worker."""
        if self._closing:
            return
        self._closing = True
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        accept_task = self._accept_task
        self._accept_task = None
        if accept_task is not None:
            accept_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await accept_task

        async with self._spawn_lock:
            children = tuple(self._children)
        for child in children:
            _terminate_process_group(child)
        if self._connection_tasks:
            await asyncio.gather(*self._connection_tasks, return_exceptions=True)
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    async def _accept_loop(self) -> None:
        listener = self._require_listener()
        loop = asyncio.get_running_loop()
        while True:
            connection, _address = await loop.sock_accept(listener)
            # asyncio accepts from a non-blocking listener. Nix's FdSource
            # uses blocking reads, so restore the daemon protocol's expected
            # descriptor mode before the exec'd worker inherits it.
            connection.setblocking(True)
            task = asyncio.create_task(self._serve_connection(connection), name="nanopynix-daemon-connection")
            self._connection_tasks.add(task)
            task.add_done_callback(self._finish_connection_task)

    async def _serve_connection(self, connection: socket.socket) -> None:
        child: asyncio.subprocess.Process | None = None
        try:
            async with self._spawn_lock:
                if self._closing:
                    return
                config_read_fd, config_write_fd = os.pipe()
                try:
                    try:
                        child = await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "nanopynix.rpc.daemon._connection",
                            "--connection-fd",
                            str(connection.fileno()),
                            "--config-fd",
                            str(config_read_fd),
                            pass_fds=(connection.fileno(), config_read_fd),
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
                    await asyncio.to_thread(_write_all, config_write_fd, self._config.to_bytes())
                except BaseException:
                    _terminate_process_group(child)
                    await child.wait()
                    raise
            connection.close()
            returncode = await child.wait()
            if returncode != 0:
                logger.warning("daemon connection worker exited with status %d", returncode)
        finally:
            connection.close()
            if child is not None:
                self._children.discard(child)

    def _finish_connection_task(self, task: asyncio.Task[None]) -> None:
        self._connection_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("daemon connection task failed")

    def _require_listener(self) -> socket.socket:
        listener = self._listener
        if listener is None:
            raise RuntimeError("daemon supervisor is not running")
        return listener
