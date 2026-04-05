"""
asyncssh SSH server that speaks the nix-daemon protocol.

Accepts SSH connections, authenticates, and spawns a DaemonProxy
for each `nix-daemon --stdio` exec request.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import asyncssh
import structlog

from . import wire
from .build_queue import BuildQueue
from .proxy import DaemonProxy
from .scheduler import Scheduler
from .store import Store
from .wire import SSHNixReader, SSHNixWriter

log = structlog.get_logger(__name__)


class _NixSSHServer(asyncssh.SSHServer):
    """Accept all authentication (development mode)."""

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        log.info("password_auth", username=username)
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        log.info(
            "pubkey_auth", username=username, key_fingerprint=key.get_fingerprint()
        )
        return True


async def start_ssh_server(
    stores: Mapping[str, Store],
    local_store: Store,
    build_queue: BuildQueue | None,
    scheduler: Scheduler | None,
    host: str = "127.0.0.1",
    port: int = 0,
    host_key_path: Path | None = None,
) -> asyncssh.SSHAcceptor:
    """Start the SSH server.

    Args:
        stores: Store instances (shared across clients)
        local_store: Shared local Store for client connections
        build_queue: Shared build queue (None in local mode)
        scheduler: Shared scheduler (None in local mode)
        host: Listen address
        port: Listen port (0 for random available port)
        host_key_path: Path to SSH host key

    Returns:
        The asyncssh acceptor instance.
    """
    # Load or generate host key
    if host_key_path and host_key_path.exists():
        host_key: asyncssh.SSHKey = asyncssh.read_private_key(str(host_key_path))
        log.info("host_key_loaded_from_file", host_key_path=host_key_path)
    else:
        host_key = asyncssh.generate_private_key("ssh-rsa", key_size=4096)
        if host_key_path:
            host_key.write_private_key(str(host_key_path))
            log.info("host_key_generated", host_key_path=host_key_path)
        else:
            log.info("host_key_ephemeral_generated")

    async def handle_client(process: asyncssh.SSHServerProcess) -> None:
        cmd: str | None = process.command
        log.info("client_exec", cmd=cmd)
        if not cmd or ("nix-daemon" not in cmd and "nix daemon" not in cmd):
            process.stderr.write(b"pynixd: unsupported command\n")
            process.exit(1)
            return

        process.channel.set_write_buffer_limits(
            high=wire._SSH_WINDOW_SIZE, low=wire._SSH_WINDOW_SIZE // 4
        )
        exit_code = 0
        try:
            proxy = DaemonProxy(
                SSHNixReader(process.stdin),
                SSHNixWriter(process.stdout),
                local_store=local_store,
                build_queue=build_queue,
                scheduler_trigger=scheduler.trigger if scheduler else None,
            )
            await proxy.run()
        except Exception:
            log.exception("proxy_session_failed")
            exit_code = 1
        finally:
            process.exit(exit_code)

    # Start the SSH server
    server = await asyncssh.listen(
        host,
        port,
        server_host_keys=[host_key],
        server_factory=_NixSSHServer,
        process_factory=handle_client,
        encoding=None,
    )
    bound_port = server.get_port()
    log.info("ssh_server_listening", host=host, port=bound_port)
    return server
