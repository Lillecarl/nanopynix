"""
Reverse store — builder-initiated reverse-SSH connection to the controller.

Each ReverseStore wraps a reverse SSH connection (SSHClientConnection from the
controller's perspective).  ``create_conn()`` opens new ``nix-daemon --stdio``
processes on the builder over this connection, providing full connection pooling
that follows the same pattern as ``SSHSubprocessStore``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from .. import wire
from ..connection import Connection
from ..store_path import StorePath
from ..wire import SSHNixReader, SSHNixWriter
from .base import ProbeState, Store

if TYPE_CHECKING:
    import asyncssh

    from ..config import ReverseStoreSpec
    from ..drv_parser import Derivation

log = structlog.get_logger(__name__)


class ReverseStore(Store):
    """A store where the builder connected TO us via reverse SSH.

    The controller holds an ``SSHClientConnection`` (obtained via
    :func:`asyncssh.listen_reverse`) and uses it to spawn
    ``nix-daemon --stdio`` processes on the builder.
    """

    def __init__(self, spec: ReverseStoreSpec, ssh_conn: asyncssh.SSHClientConnection) -> None:
        super().__init__(spec)
        self._ssh_conn: asyncssh.SSHClientConnection = ssh_conn
        self.nix_bin = spec.nix_bin

    async def create_conn(self) -> Connection:
        conn_id = f"{self.store_id}-{self.conn_counter}"
        cmd = "nix-daemon --stdio" if self.nix_bin == "nix" else f"{self.nix_bin} daemon --stdio"
        log.debug(
            "reverse_spawning_remote_daemon",
            cmd=cmd,
            conn_id=conn_id,
            store_id=self.store_id,
        )
        proc = await self._ssh_conn.create_process(cmd, encoding=None)
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

    async def read_derivation(self, drv_store_path: StorePath | str) -> Derivation | None:
        """Fetch and parse a .drv file via nix-daemon protocol over reverse SSH."""
        from ..drv_parser import parse_drv
        from ..nar import NarRegular, parse_nar
        from ..operations.is_valid_path import IsValidPathRequest
        from ..operations.nar_from_path import NarFromPathRequest

        sp = StorePath(str(drv_store_path))

        valid = (await IsValidPathRequest(path=sp).execute(self)).valid
        if not valid:
            log.warning(
                "drv_not_found",
                drv_path=str(drv_store_path),
                reason="not_valid_on_remote",
            )
            return None

        resp = await NarFromPathRequest(path=sp, nar_size=0).execute(self)
        if not resp.nar_data:
            log.warning(
                "drv_not_found",
                drv_path=str(drv_store_path),
                reason="nar_empty",
            )
            return None

        node = parse_nar(resp.nar_data)
        if not isinstance(node, NarRegular):
            return None

        return parse_drv(node.contents.decode())

    async def probe(self) -> None:
        """Mark as probed immediately — metadata comes from the registration handshake."""
        if self.probe_state == ProbeState.PROBED:
            return
        self.probe_state = ProbeState.PROBED
        self._probe_event.set()
