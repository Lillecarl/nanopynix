"""
Local Nix daemon store implementation.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import shlex
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..config import PynixdSettings
from ..connection import Connection
from ..monitor import DummyResourceMonitor, create_monitor
from ..types.ids import StoreId
from ..wire import UnixNixReader, UnixNixWriter
from .base import Store

if TYPE_CHECKING:
    from ..monitor import ResourceMonitor

log = structlog.get_logger(__name__)

DAEMON_SOCKET_PATH = Path("/nix/var/nix/daemon-socket/socket")


class LocalSocketStore(Store):
    """Connects to local nix-daemon via Unix socket.

    Can optionally spawn and manage its own daemon subprocess with a
    custom --store path. The socket is placed at
    ``<store_path>/var/nix/daemon-socket/socket`` and the daemon is
    told about it via NIX_DAEMON_SOCKET_PATH.

    If no store_path is given (or store_path="/"), connects to the
    system daemon socket without spawning anything.
    """

    def __init__(
        self,
        store_id: str | StoreId,
        store_path: Path | None = None,
        socket_path: Path | None = None,
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
        nix_bin: str = "nix",
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        use_db: bool = True,
        monitor: bool = True,
        settings: PynixdSettings | None = None,
    ) -> None:
        if store_path is None:
            store_path = Path("/")
        managed = store_path != Path("/")
        if socket_path:
            self.socket_path = socket_path
        elif managed:
            self.socket_path = store_path / "var" / "nix" / "daemon-socket" / "socket"
        else:
            self.socket_path = DAEMON_SOCKET_PATH

        super().__init__(
            store_id=StoreId(store_id),
            store_path=store_path,
            feature_matrix=feature_matrix,
            probe=probe,
        )
        self.managed = managed
        self.nix_bin = nix_bin
        self.use_db = use_db
        self.monitor_enabled = monitor
        self.daemon_proc: asyncio.subprocess.Process | None = None
        self.daemon_ready: asyncio.Event | None = None
        self._daemon_log_task: asyncio.Task | None = None
        self.extra_env = extra_env or {}
        self.extra_args = extra_args or []
        self.settings = settings or PynixdSettings()

        # Register atexit handler to ensure daemon is killed even if close() is never called
        if self.managed:
            atexit.register(self._atexit_kill_daemon)

        # Resource Monitoring
        self.monitor: ResourceMonitor | None = (
            create_monitor(self.gate, self.settings)
            if self.monitor_enabled
            else DummyResourceMonitor(self.gate, self.settings)
        )

    @property
    def db_enabled(self) -> bool:
        return self.use_db

    async def start(self) -> None:
        """Spawn managed daemon and initialize the store."""
        await self.ensure_daemon()
        await super().start()

    async def ensure_daemon(self) -> None:
        """Ensure a daemon is reachable, spawning one if needed.

        For managed stores (store_path != /), always spawns a daemon.

        For unmanaged stores (store_path = /), probes the socket first.
        If the socket is already accepting connections, no daemon is
        spawned. Otherwise, auto-spawns a daemon and owns its lifecycle.

        Uses an Event to coordinate concurrent callers.
        """
        if self.daemon_proc is not None:
            if self.daemon_ready is not None:
                await self.daemon_ready.wait()
            return

        if not self.managed:
            if await self._probe_socket():
                return
            log.info(
                "socket_not_reachable_auto_spawning",
                socket_path=str(self.socket_path),
            )

        self.daemon_ready = asyncio.Event()

        if self.socket_path.exists():
            log.info("removing_stale_socket", socket_path=str(self.socket_path))
            self.socket_path.unlink()

        path = self.store_path or Path("/")
        socket_dir = self.socket_path.parent
        socket_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.nix_bin,
            "daemon",
            "--store",
            str(path),
            "--option",
            "build-dir",
            str(path / "nix" / "var" / "nix" / "builds"),
        ]
        cmd.extend(self.extra_args)

        log.info(
            "spawning_managed_daemon",
            nix_bin=self.nix_bin,
            store_path=str(path),
            socket_path=str(self.socket_path),
            cmd=shlex.join(cmd),
        )
        env = os.environ.copy()
        env.update(self.extra_env)
        env["NIX_DAEMON_SOCKET_PATH"] = str(self.socket_path)
        env["NIX_DATA_DIR"] = str(self.store_path / "share")
        env["NIX_LOG_DIR"] = str(self.store_path / "var/log/nix")
        env["NIX_STATE_DIR"] = str(self.store_path / "var/nix")

        self.daemon_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        if self.daemon_proc.stdout:
            self._daemon_log_task = asyncio.create_task(
                self._stream_daemon_output(self.daemon_ready),
                name="daemon-log-forwarder",
            )

        # Wait for socket file to appear
        for _ in range(100):
            if self.socket_path.exists():
                break
            if self.daemon_proc.returncode is not None:
                stderr_output = ""
                if self.daemon_proc.stderr:
                    stderr_output = (await self.daemon_proc.stderr.read()).decode(errors="replace")
                raise RuntimeError(
                    f"Managed daemon exited early with code {self.daemon_proc.returncode} "
                    f"(pid={self.daemon_proc.pid}): {stderr_output!r}",
                )
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(
                f"Managed daemon did not create socket at {self.socket_path} within 10s (pid={self.daemon_proc.pid})",
            )

        # Socket file exists but daemon may not be listening yet — probe
        for _attempt in range(100):
            if self.daemon_proc.returncode is not None:
                stderr_output = ""
                if self.daemon_proc.stderr:
                    stderr_output = (await self.daemon_proc.stderr.read()).decode(errors="replace")
                raise RuntimeError(
                    f"Managed daemon exited with code {self.daemon_proc.returncode} "
                    f"(pid={self.daemon_proc.pid}): {stderr_output!r}",
                )
            if await self._probe_socket():
                log.info("daemon_socket_ready", socket_path=str(self.socket_path))
                if self._daemon_log_task and not self._daemon_log_task.done():
                    self._daemon_log_task.cancel()
                await asyncio.sleep(0.1)
                self.daemon_ready.set()
                return
            await asyncio.sleep(0.05)

        stderr_output = ""
        if self.daemon_proc.stderr:
            stderr_output = (await self.daemon_proc.stderr.read()).decode(errors="replace")
        raise RuntimeError(
            f"Managed daemon socket not accepting connections "
            f"at {self.socket_path} within 5s (pid={self.daemon_proc.pid}): {stderr_output!r}",
        )

    async def _stream_daemon_output(self, stop: asyncio.Event) -> None:
        """Forward daemon stdout/stderr to structlog until stop is set."""
        if not self.daemon_proc or not self.daemon_proc.stdout or not self.daemon_proc.stderr:
            return

        async def _forward(stream: asyncio.StreamReader | None, label: str) -> None:
            if stream is None:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                log.info(f"daemon_{label}", msg=line.decode(errors="replace").rstrip())
                if stop.is_set():
                    return

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_forward(self.daemon_proc.stdout, "stdout"))
            tg.create_task(_forward(self.daemon_proc.stderr, "stderr"))

    async def _probe_socket(self) -> bool:
        """Perform a full daemon handshake to verify a live Nix daemon.

        Connects to socket_path, does the protocol handshake, and cleanly
        closes. Returns True only if a real Nix daemon responded.
        """
        try:
            r, w = await asyncio.open_unix_connection(str(self.socket_path))
        except (ConnectionRefusedError, ConnectionResetError, FileNotFoundError, OSError):
            return False

        conn = Connection(
            UnixNixReader(r, identifier="probe"),
            UnixNixWriter(w, identifier="probe"),
            "probe",
            store_path=self.store_path,
        )
        try:
            await conn.connect()
        except (OSError, EOFError, ConnectionError):
            log.debug("probe_connection_failed", exc_info=True)
            with contextlib.suppress(Exception):
                await conn.close()
            return False
        await conn.close()
        return True

    async def create_conn(self) -> Connection:
        await self.ensure_daemon()
        conn_id = f"{self.store_id}-{self.conn_counter}"
        log.debug(
            "connecting_daemon_socket",
            socket_path=str(self.socket_path),
            conn_id=conn_id,
        )
        r, w = await asyncio.open_unix_connection(str(self.socket_path))
        conn = Connection(
            UnixNixReader(r, identifier=conn_id),
            UnixNixWriter(w, identifier=conn_id),
            conn_id,
            store_path=self.store_path,
        )
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores, stop monitor and terminate managed daemon if any."""
        await super().close()
        if self.monitor:
            await self.monitor.stop()
        if self._daemon_log_task and not self._daemon_log_task.done():
            self._daemon_log_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._daemon_log_task
        await self._kill_daemon()

    async def _kill_daemon(self) -> None:
        """Terminate the managed daemon process and all its children.

        The daemon is spawned with `start_new_session=True`, making it a
        session leader. Its PID equals the process group ID, so we can
        kill the entire group with `os.killpg`.
        """
        # Unregister atexit handler so we don't double-kill
        if self.managed:
            with contextlib.suppress(ValueError):
                atexit.unregister(self._atexit_kill_daemon)

        if self.daemon_proc is None:
            return
        pid = self.daemon_proc.pid
        log.info("terminating_daemon_process_group", pid=pid)

        # SIGTERM the entire process group (daemon + any children)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)

        try:
            await asyncio.wait_for(self.daemon_proc.wait(), timeout=5.0)
        except TimeoutError:
            log.warning("daemon_sigterm_timeout_escalating_to_sigkill", pid=pid)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)

            # Final wait so the process is reaped
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self.daemon_proc.wait(), timeout=2.0)

        self.daemon_proc = None
        self.daemon_ready = None

    def _atexit_kill_daemon(self) -> None:
        """Synchronous atexit handler to kill the daemon process group.
        This is called when the Python process exits without close() having
        been invoked (e.g., unhandled exception, os._exit, or interpreter crash).
        """
        proc = self.daemon_proc
        if proc is None:
            return

        pid = proc.pid
        log.info("atexit_killing_daemon_process_group", pid=pid)

        # Best-effort SIGTERM then SIGKILL synchronously
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)

        # Very brief synchronous wait — atexit is synchronous, can't asyncio
        time.sleep(0.5)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
