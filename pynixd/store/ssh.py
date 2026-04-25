"""
SSH Store implementations for pynixd.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

import asyncssh
import structlog

from .. import wire
from ..config import PynixdSettings
from ..connection import Connection
from ..monitor import DummyResourceMonitor, GenericResourcePoller, ResourceMonitor
from ..psi import (
    CpuUtil,
    MemInfo,
)
from ..wire import SSHNixReader, SSHNixWriter
from .base import Store

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


class _SSHStoreMixin(Store):
    """Shared SSH connection management with exponential backoff reconnection.

    Subclasses must set host, port, username on __init__.
    """

    host: str
    port: int
    username: str | None
    client_keys: list[str | Path | asyncssh.SSHKey] | None
    conn: asyncssh.SSHClientConnection | None
    backoff: float
    max_backoff: float
    last_failure: float
    id: str

    INITIAL_BACKOFF: float = 1.0
    MAX_BACKOFF: float = 60.0

    def init_ssh_state(
        self,
        *,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
        settings: PynixdSettings | None = None,
    ) -> None:
        self.conn = None
        self.ssh_lock = asyncio.Lock()
        self.backoff = self.INITIAL_BACKOFF
        self.max_backoff = self.MAX_BACKOFF
        self.last_failure = 0.0
        self.monitor_enabled = monitor
        self.client_keys = client_keys
        self.settings = settings or PynixdSettings()
        # self.gate is inherited from Store.__init__
        self.monitor: ResourceMonitor | None = None

    async def start(self) -> None:
        """Establish SSH connection and initialize the store."""
        await self.ensure_ssh()
        await super().start()

    def start_psi_polling(self, sftp: asyncssh.SFTPClient) -> None:
        """Start consolidated resource poller over SFTP."""
        if not self.monitor_enabled:
            # If monitoring is explicitly disabled, use dummy monitor with 0.0 load
            if self.monitor is None:
                self.monitor = DummyResourceMonitor(self.gate, self.settings)
                self.monitor.start()
            return

        async def sftp_read(path: str) -> str:
            async with sftp.open(path, "r") as f:
                return await f.read()

        async def sftp_exists(path: str) -> bool:
            try:
                await sftp.stat(path)
                return True
            except asyncssh.SFTPError:
                return False

        if self.monitor is None or isinstance(self.monitor, DummyResourceMonitor):
            if self.monitor:
                asyncio.create_task(self.monitor.stop())
            self.monitor = GenericResourcePoller(
                self.gate,
                self.settings,
                sftp_read,
                sftp_exists,
            )
            self.monitor.start()

    def stop_psi_polling(self) -> None:
        """Cancel the resource polling task."""
        if self.monitor is not None:
            asyncio.create_task(self.monitor.stop())
            self.monitor = None

    @property
    def pressure(self) -> float | None:
        """System pressure score (0-100), or None if unavailable."""
        if self.monitor is None:
            return None

        if isinstance(self.monitor, DummyResourceMonitor):
            return 0.0

        if self.monitor.health.psi is None:
            return None

        # Stale check: 3x interval
        interval = getattr(self.monitor, "interval", 5.0)
        if time.monotonic() - self.monitor.health.timestamp > interval * 3:
            return None
        return self.monitor.health.psi.pressure_score()

    @property
    def meminfo(self) -> MemInfo | None:
        """System memory info, or None if unavailable."""
        return self.monitor.health.meminfo if self.monitor else None

    @property
    def cpu_util(self) -> CpuUtil | None:
        """CPU utilization from cgroupv2, or None if unavailable."""
        return self.monitor.health.cpu_util if self.monitor else None

    async def ensure_ssh(self) -> asyncssh.SSHClientConnection:
        if self.conn is not None:
            return self.conn

        async with self.ssh_lock:
            # Re-check after acquiring lock (another task may have connected)
            if self.conn is not None:
                return self.conn

            # Respect backoff from previous failure
            now = time.monotonic()
            wait = self.last_failure + self.backoff - now
            if self.last_failure > 0 and wait > 0:
                log.info("ssh_backoff", store_id=self.id, backoff_seconds=wait)
                await asyncio.sleep(wait)

            try:
                log.info(
                    "ssh_connecting",
                    username=self.username or "",
                    host=self.host,
                    port=self.port,
                )
                self.conn = await asyncssh.connect(
                    self.host,
                    port=self.port,
                    username=self.username,
                    client_keys=self.client_keys,
                    known_hosts=None,
                )
                # Reset backoff on success
                self.backoff = self.INITIAL_BACKOFF
                self.last_failure = 0.0
                self.record_success()

                if self.monitor_enabled:
                    sftp = await self.conn.start_sftp_client()
                    self.start_psi_polling(sftp)

                return self.conn
            except Exception:
                self.last_failure = time.monotonic()
                self.backoff = min(self.backoff * 2, self.MAX_BACKOFF)
                self.record_failure()
                log.warning(
                    "ssh_connect_failed",
                    store_id=self.id,
                    next_retry_seconds=self.backoff,
                )
                raise

    def invalidate_ssh(self) -> None:
        """Mark SSH connection as dead so next ensure_ssh reconnects."""
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    async def close_ssh(self) -> None:
        self.stop_psi_polling()
        if self.conn is not None:
            self.conn.close()
            self.conn = None


class SSHSubprocessStore(_SSHStoreMixin):
    """Persistent SSH connection, spawns nix-daemon --stdio channels.

    Used primarily for "fake Nix" stores like nixbuild.net that provide
    a nix-daemon protocol over stdin/stdout. For real Nix stores over SSH,
    SSHSocketStore (tunnelling to a Unix socket) is preferred.

    If store_path is set, runs ``nix daemon --store <path> --stdio``.
    Otherwise runs ``nix-daemon --stdio`` (default store, nixbuild.net compat).
    """

    def __init__(
        self,
        host: str,
        id: str | None = None,
        port: int = 22,
        username: str | None = None,
        store_path: Path = Path("/"),
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
        nix_bin: str = "nix",
    ) -> None:
        super().__init__(
            id=id or f"ssh:{username or ''}@{host}:{port}",
            store_path=store_path,
            feature_matrix=feature_matrix,
            probe=probe,
        )
        self.host = host
        self.port = port
        self.username = username
        self.nix_bin = nix_bin
        self.init_ssh_state(monitor=monitor, client_keys=client_keys)
        self.ssh_processes: list[asyncssh.SSHClientProcess] = []

    async def create_conn(self) -> Connection:
        try:
            ssh_conn = await self.ensure_ssh()
        except Exception:
            raise
        conn_id = f"{self.id}-{self.conn_counter}"
        if self.store_path and self.store_path != Path("/"):
            cmd = f"{self.nix_bin} daemon --store {self.store_path} --stdio"
        elif self.nix_bin != "nix":
            cmd = f"{self.nix_bin} daemon --stdio"
        else:
            cmd = "nix-daemon --stdio"
        log.debug(
            "spawning_remote_daemon",
            cmd=cmd,
            conn_id=conn_id,
        )
        try:
            proc = await ssh_conn.create_process(cmd, encoding=None)
        except Exception:
            self.invalidate_ssh()
            raise
        self.ssh_processes.append(proc)
        proc.channel.set_write_buffer_limits(
            high=wire._SSH_WINDOW_SIZE,
            low=wire._SSH_WINDOW_SIZE // 4,
        )

        conn = Connection(
            SSHNixReader(proc.stdout, identifier=conn_id),
            SSHNixWriter(proc.stdin, identifier=conn_id),
            conn_id,
        )
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores, SSH processes, and SSH connection."""
        await super().close()
        for proc in self.ssh_processes:
            try:
                proc.terminate()
            except Exception:
                pass
            proc.close()
        self.ssh_processes.clear()
        await self.close_ssh()


DAEMON_SOCKET_PATH = Path("/nix/var/nix/daemon-socket/socket")


class SSHSocketStore(_SSHStoreMixin):
    """Persistent SSH connection, tunnels to remote Unix socket."""

    def __init__(
        self,
        host: str,
        id: str | None = None,
        port: int = 22,
        username: str | None = None,
        socket_path: Path = DAEMON_SOCKET_PATH,
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
    ) -> None:
        super().__init__(
            id=id or f"ssh-socket:{username or ''}@{host}:{port}",
            feature_matrix=feature_matrix,
            probe=probe,
        )
        self.host = host
        self.port = port
        self.username = username
        self.socket_path = socket_path
        self.init_ssh_state(monitor=monitor, client_keys=client_keys)

    async def create_conn(self) -> Connection:
        try:
            ssh_conn = await self.ensure_ssh()
        except Exception:
            raise
        conn_id = f"{self.id}-{self.conn_counter}"
        log.debug(
            "tunneling_to_socket",
            socket_path=str(self.socket_path),
            conn_id=conn_id,
        )
        try:
            r, w = await ssh_conn.open_unix_connection(str(self.socket_path))
        except Exception:
            self.invalidate_ssh()
            raise
        conn = Connection(
            SSHNixReader(r, identifier=conn_id),
            SSHNixWriter(w, identifier=conn_id),
            conn_id,
        )
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores and SSH connection."""
        await super().close()
        await self.close_ssh()
