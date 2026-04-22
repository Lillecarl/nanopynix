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
from environs import env

from .base import Store
from .. import wire
from ..connection import Connection
from ..wire import SSHNixReader, SSHNixWriter
from ..psi import (
    CgroupCpuStat,
    CpuUtil,
    MemInfo,
    PsiSnapshot,
    compute_cpu_util,
    count_cpus_from_proc_stat,
    parse_cpu_max,
    parse_cpu_stat,
    parse_psi_output,
)

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
    PSI_INTERVAL = env.float("PYNIXD_PSI_INTERVAL", 5.0)

    def init_ssh_state(
        self,
        *,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
    ) -> None:
        self.conn = None
        self.ssh_lock = asyncio.Lock()
        self.backoff = self.INITIAL_BACKOFF
        self.max_backoff = self.MAX_BACKOFF
        self.last_failure = 0.0
        self.monitor_enabled = monitor
        self.client_keys = client_keys
        self.psi_data: PsiSnapshot | None = None
        self.meminfo_data: MemInfo | None = None
        self.cpu_stat_prev: CgroupCpuStat | None = None
        self.cpu_stat_curr: CgroupCpuStat | None = None
        self.cpu_cores: float | None = None
        self.cpu_util_data: CpuUtil | None = None
        self.psi_task: asyncio.Task[None] | None = None

    def start_psi_polling(self) -> None:
        """Start PSI polling loop. Called after first successful SSH connect."""
        if not self.monitor_enabled:
            return
        if self.psi_task is None or self.psi_task.done():
            self.psi_task = asyncio.create_task(self.psi_poll_loop())

    PSI_FILES: tuple[str, ...] = (
        "/sys/fs/cgroup/cpu.pressure",
        "/sys/fs/cgroup/memory.pressure",
        "/sys/fs/cgroup/io.pressure",
    )

    async def psi_poll_loop(self) -> None:
        """Periodically read PSI, meminfo, and cpu stats over SFTP.

        All paths are cgroupv2 under /sys/fs/cgroup/. If cgroupv2 is not
        available, polling stops gracefully.
        """
        while True:
            try:
                conn = self.conn
                if conn is None:
                    await asyncio.sleep(self.PSI_INTERVAL)
                    continue
                async with conn.start_sftp_client() as sftp:
                    # Read cpu.max once — the quota rarely changes
                    # Fall back to /proc/stat for nproc if no cpu.max (root cgroup)
                    if self.cpu_cores is None:
                        try:
                            async with sftp.open("/sys/fs/cgroup/cpu.max", "r") as f:
                                self.cpu_cores = parse_cpu_max(await f.read())
                        except asyncssh.SFTPError:
                            pass
                        if self.cpu_cores is None:
                            try:
                                async with sftp.open("/proc/stat", "r") as f:
                                    self.cpu_cores = float(
                                        count_cpus_from_proc_stat(await f.read())
                                    )
                            except asyncssh.SFTPError:
                                pass

                    while True:
                        parts = []
                        for path in self.PSI_FILES:
                            async with sftp.open(path, "r") as f:
                                parts.append(await f.read())
                        self.psi_data = parse_psi_output("".join(parts))

                        try:
                            async with sftp.open("/sys/fs/cgroup/cpu.stat", "r") as f:
                                stat = parse_cpu_stat(await f.read())
                            self.cpu_stat_prev = self.cpu_stat_curr
                            self.cpu_stat_curr = stat
                            if (
                                self.cpu_stat_prev is not None
                                and self.cpu_stat_curr is not None
                            ):
                                self.cpu_util_data = compute_cpu_util(
                                    self.cpu_stat_prev,
                                    self.cpu_stat_curr,
                                    self.cpu_cores,
                                )
                        except asyncssh.SFTPError:
                            pass

                        try:
                            async with sftp.open(
                                "/sys/fs/cgroup/memory.current", "r"
                            ) as f:
                                mem_current = int((await f.read()).strip())
                            async with sftp.open("/sys/fs/cgroup/memory.max", "r") as f:
                                mem_max_raw = (await f.read()).strip()
                            mem_max = None if mem_max_raw == "max" else int(mem_max_raw)
                            try:
                                async with sftp.open(
                                    "/sys/fs/cgroup/swap.current", "r"
                                ) as f:
                                    swap_current = int((await f.read()).strip())
                            except asyncssh.SFTPError:
                                swap_current = 0
                            try:
                                async with sftp.open(
                                    "/sys/fs/cgroup/swap.max", "r"
                                ) as f:
                                    swap_max_raw = (await f.read()).strip()
                                swap_max = (
                                    None if swap_max_raw == "max" else int(swap_max_raw)
                                )
                            except asyncssh.SFTPError:
                                swap_max = None
                            self.meminfo_data = MemInfo(
                                mem_total=mem_max if mem_max else 0,
                                mem_available=(mem_max - mem_current if mem_max else 0),
                                swap_total=(swap_max if swap_max is not None else 0),
                                swap_free=(
                                    (swap_max - swap_current)
                                    if swap_max is not None
                                    else 0
                                ),
                            )
                        except asyncssh.SFTPError:
                            pass

                        await asyncio.sleep(self.PSI_INTERVAL)
            except asyncio.CancelledError:
                return
            except (
                asyncssh.SFTPNoSuchFile,
                asyncssh.SFTPPermissionDenied,
                asyncssh.SFTPOpUnsupported,
            ) as e:
                # PSI not available on this host (macOS, old kernel, restricted perms)
                log.info("psi_unavailable", store_id=self.id, error=e)
                self.psi_data = None
                return
            except asyncssh.SFTPConnectionLost:
                # SFTP channel died, retry after SSH reconnects
                log.debug("psi_sftp_lost", store_id=self.id)
                self.psi_data = None
                await asyncio.sleep(self.PSI_INTERVAL)
            except asyncssh.SFTPError as e:
                # Any other SFTP error — probably not recoverable
                log.info("psi_sftp_error", store_id=self.id, error=str(e))
                self.psi_data = None
                return
            except (asyncssh.Error, OSError) as e:
                # SSH connection-level error — retry, SSH reconnect may fix it
                log.debug("psi_ssh_error", store_id=self.id, error=str(e))
                self.psi_data = None
                await asyncio.sleep(self.PSI_INTERVAL)

    def stop_psi_polling(self) -> None:
        """Cancel the PSI polling task."""
        if self.psi_task is not None:
            self.psi_task.cancel()
            self.psi_task = None

    @property
    def pressure(self) -> float | None:
        """System pressure score (0-100), or None if unavailable."""
        if self.psi_data is None:
            return None
        # Stale check: 3x interval
        if time.monotonic() - self.psi_data.timestamp > self.PSI_INTERVAL * 3:
            return None
        return self.psi_data.pressure_score()

    @property
    def meminfo(self) -> MemInfo | None:
        """System memory info, or None if unavailable."""
        return self.meminfo_data

    @property
    def cpu_util(self) -> CpuUtil | None:
        """CPU utilization from cgroupv2, or None if unavailable."""
        return self.cpu_util_data

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
                self.start_psi_polling()
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
        max_builds: int = 2,
        max_transfers: int = 4,
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
        nix_bin: str = "nix",
    ) -> None:
        super().__init__(
            id=id or f"ssh:{username or ''}@{host}:{port}",
            store_path=store_path,
            max_builds=max_builds,
            max_transfers=max_transfers,
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
            high=wire._SSH_WINDOW_SIZE, low=wire._SSH_WINDOW_SIZE // 4
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
        max_builds: int = 2,
        max_transfers: int = 4,
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
    ) -> None:
        super().__init__(
            id=id or f"ssh-socket:{username or ''}@{host}:{port}",
            max_builds=max_builds,
            max_transfers=max_transfers,
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
