from __future__ import annotations

import contextlib
import os
import socket
import tempfile
from typing import Any

import asyncssh
import pytest
from _bench_utils import LARGE_COUNT, LARGE_PAYLOAD, SMALL_COUNT, SMALL_PAYLOAD, bench, run_bench
from anyio import Path
from grpclib_transports.example.server import Greeter
from grpclib_transports.protocol import DEFAULT_TUNING, serve_h2
from grpclib_transports.ssh import SshChannel, SshTransport


@pytest.mark.parametrize("parallelism", [1, 2, 4, 8])
def test_ssh(parallelism: int) -> None:
    class _Server(asyncssh.SSHServer):
        def password_auth_supported(self):
            return True

        def validate_password(self, username: str, password: str):
            return True

    async def run():
        fd, sock = tempfile.mkstemp(suffix=".sock")
        os.close(fd)
        await Path(sock).unlink()
        key = asyncssh.generate_private_key("ssh-ed25519")  # pyright: ignore[reportUnknownMemberType] -- asyncssh type stubs are incomplete
        ssock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ssock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, DEFAULT_TUNING.buffer_size)
        ssock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, DEFAULT_TUNING.buffer_size)
        ssock.bind(sock)
        ssock.listen(100)

        async def session_handler(stdin: Any, stdout: Any, _stderr: Any):
            t = SshTransport(stdin, stdout)
            await serve_h2([Greeter()], stdin, t)

        acceptor = await asyncssh.listen(
            sock=ssock,
            server_host_keys=[key],
            server_factory=_Server,
            session_factory=session_handler,
            encoding=None,
            line_editor=False,
            encryption_algs=[
                "aes256-gcm@openssh.com",
                "aes128-gcm@openssh.com",
                "aes256-ctr",
                "aes192-ctr",
                "aes128-ctr",
                "chacha20-poly1305@openssh.com",
            ],
        )
        try:
            csock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            csock.connect(sock)
            conn = await asyncssh.connect(
                sock=csock,
                known_hosts=None,
                username="x",
                password="x",
                encryption_algs=[
                    "aes256-gcm@openssh.com",
                    "aes128-gcm@openssh.com",
                    "aes256-ctr",
                    "aes192-ctr",
                    "aes128-ctr",
                    "chacha20-poly1305@openssh.com",
                ],
            )
            try:
                stdin, stdout, _ = await conn.open_session(encoding=None)  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType] -- asyncssh type stubs are incomplete
                channel = SshChannel(stdout, stdin)
                await bench(
                    f"ssh (small, p={parallelism})", SMALL_PAYLOAD, SMALL_COUNT, channel, parallelism=parallelism
                )
                await bench(
                    f"ssh (large, p={parallelism})", LARGE_PAYLOAD, LARGE_COUNT, channel, parallelism=parallelism
                )
                await channel.aclose()
            finally:
                conn.close()
        finally:
            acceptor.close()
            await acceptor.wait_closed()
            with contextlib.suppress(OSError):
                await Path(sock).unlink()

    run_bench(f"ssh (p={parallelism})", run())
