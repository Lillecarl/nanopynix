"""
Local Nix daemon store implementation.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

import structlog

from .base import Store
from ..connection import Connection
from ..wire import UnixNixReader, UnixNixWriter

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
        id: str | None = None,
        store_path: Path | None = None,
        socket_path: Path | None = None,
        max_builds: int = 1,
        max_transfers: int = 4,
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
        nix_bin: str = "nix",
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        use_db: bool = True,
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
            id=id or f"local-socket:{self.socket_path}",
            store_path=store_path,
            max_builds=max_builds,
            max_transfers=max_transfers,
            feature_matrix=feature_matrix,
            probe=probe,
        )
        self.managed = managed
        self.nix_bin = nix_bin
        self.use_db = use_db
        self.daemon_proc: asyncio.subprocess.Process | None = None
        self.daemon_ready: asyncio.Event | None = None
        self.extra_env = extra_env or {}
        self.extra_args = extra_args or []

    @property
    def db_enabled(self) -> bool:
        return self.use_db

    async def ensure_daemon(self) -> None:
        """Spawn a managed daemon if needed (first call only).

        Uses an Event to coordinate concurrent callers — only the first
        spawns the daemon; others wait for it to be ready.
        """
        if not self.managed:
            return
        if self.daemon_proc is not None:
            # Daemon already spawned — wait for it to be ready
            if self.daemon_ready is not None:
                await self.daemon_ready.wait()
            return

        self.daemon_ready = asyncio.Event()

        path = self.store_path or Path("/")
        socket_dir = self.socket_path.parent
        os.makedirs(socket_dir, exist_ok=True)

        cmd = [
            self.nix_bin,
            "daemon",
            "--store",
            str(path),
            "--option",
            "build-dir",
            str(path / "tmp" / "nix-builds"),
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
        # env["NIX_STORE_DIR"] = str(self.store_path / "store") # this one is evil and should not be changed

        self.daemon_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Wait for socket file to appear
        for _ in range(100):
            if self.socket_path.exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(
                f"Managed daemon did not create socket at {self.socket_path} "
                f"within 5s (pid={self.daemon_proc.pid})",
            )

        # Socket file exists but daemon may not be listening yet — probe
        for attempt in range(50):
            try:
                r, w = await asyncio.open_unix_connection(str(self.socket_path))
                w.close()
                await w.wait_closed()
                log.info("daemon_socket_ready", socket_path=str(self.socket_path))
                self.daemon_ready.set()
                return
            except (ConnectionRefusedError, ConnectionResetError):
                await asyncio.sleep(0.2)

        raise RuntimeError(
            f"Managed daemon socket exists but not accepting connections "
            f"at {self.socket_path} within 5s (pid={self.daemon_proc.pid})",
        )

    async def create_conn(self) -> Connection:
        await self.ensure_daemon()
        conn_id = f"{self.id}-{self.conn_counter}"
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
        """Close stores and terminate managed daemon if any."""
        await super().close()
        if self.managed and self.daemon_proc is not None:
            self.daemon_proc.terminate()
            try:
                await asyncio.wait_for(self.daemon_proc.wait(), timeout=5.0)
            except TimeoutError:
                self.daemon_proc.kill()
