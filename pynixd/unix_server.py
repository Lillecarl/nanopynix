"""
Unix socket server that speaks the nix-daemon protocol.

Accepts connections on a Unix domain socket and spawns a DaemonProxy
for each client. Used for testing (avoids SSH) and local daemon mode.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pyinstrument
import structlog

from .config import ScheduleMode
from .operations.base import Role
from .proxy import DaemonProxy
from .wire import UnixNixReader, UnixNixWriter

if TYPE_CHECKING:
    from .context import PynixdContext

log = structlog.get_logger(__name__)


async def start_unix_server(
    ctx: PynixdContext,
    socket_path: Path,
    schedule_mode: ScheduleMode | None = None,
) -> asyncio.Server:
    """Start a Unix socket server.

    Args:
        ctx: Shared application context
        socket_path: Path for the Unix domain socket
        schedule_mode: Scheduling mode for this listener

    Returns:
        The asyncio.Server instance.
    """

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername") or "unknown"
        log.info("unix_client_connected", peer=peer)
        profiler = pyinstrument.Profiler(async_mode="enabled")
        profiler.start()
        try:
            proxy = DaemonProxy(
                UnixNixReader(reader, identifier="client"),
                UnixNixWriter(writer, identifier="client"),
                ctx=ctx,
                role=Role.ADMIN,
                username="local",
                schedule_mode=schedule_mode or ScheduleMode.auto,
            )
            await proxy.run()
        except Exception:
            log.exception("unix_proxy_session_failed")
        finally:
            profiler.stop()
            ts = int(time.time() * 1000)
            out = Path("/tmp") / f"pynixd-profile-{ts}.txt"
            out.write_text(profiler.output_text(unicode=True, color=False, show_all=True))
            log.info("profile_dumped", path=str(out))
            writer.close()

    # Clean up stale socket
    sock = anyio.Path(socket_path)
    if await sock.exists():
        await sock.unlink()

    server = await asyncio.start_unix_server(handle_client, path=str(socket_path), limit=2**18)
    await sock.chmod(0o666)
    log.info("unix_server_listening", socket_path=socket_path)
    return server
