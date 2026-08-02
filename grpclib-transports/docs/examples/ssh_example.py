"""SSH transport — in-process server and client over SSH on a Unix socket.

Requires asyncssh.

Run with::

    python docs/examples/ssh_example.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import tempfile
from typing import Any

import greeter.greeter.common as common_pb2
import greeter.greeter.server as server_grpc
from anyio import Path
from grpclib_transports import SshChannel, SshTransport
from grpclib_transports.protocol import serve_h2
from services import Greeter


async def main() -> None:
    import asyncssh

    fd, sock = tempfile.mkstemp(suffix=".sock")
    os.close(fd)
    await Path(sock).unlink()
    key = asyncssh.generate_private_key("ssh-ed25519")  # pyright: ignore[reportUnknownMemberType] -- asyncssh type stubs are incomplete

    class _Server(asyncssh.SSHServer):
        def password_auth_supported(self):
            return True

        def validate_password(self, username: str, password: str):
            return True

    async def session_handler(stdin: Any, stdout: Any, _stderr: Any):
        transport = SshTransport(stdin, stdout)
        await serve_h2([Greeter()], stdin, transport)

    ssock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ssock.bind(sock)
    ssock.listen()
    try:
        acceptor = await asyncssh.listen(
            sock=ssock,
            server_host_keys=[key],
            server_factory=_Server,
            session_factory=session_handler,
            encoding=None,
            line_editor=False,
        )
        try:
            csock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            csock.connect(sock)
            conn = await asyncssh.connect(
                sock=csock,
                known_hosts=None,
                username="x",
                password="x",
            )
            channel = None
            try:
                stdin, stdout, _ = await conn.open_session(encoding=None)  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType] -- asyncssh type stubs are incomplete
                channel = SshChannel(stdout, stdin)
                stub = server_grpc.GreeterStub(channel)
                response = await stub.say_hello(common_pb2.HelloRequest(name="SSH"))
                assert response.message == "Hello, SSH!"
                print(f"Greeter replied: {response.message}")
            finally:
                if channel is not None:
                    await channel.aclose()
                conn.close()
        finally:
            acceptor.close()
            await acceptor.wait_closed()
    finally:
        with contextlib.suppress(OSError):
            await Path(sock).unlink()


if __name__ == "__main__":
    asyncio.run(main())
