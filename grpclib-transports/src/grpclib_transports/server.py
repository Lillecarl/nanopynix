"""Application-style server container with per-endpoint service sets."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

from grpclib.server import Server as GrpclibServer

from grpclib_transports.limits import limit_services_concurrency
from grpclib_transports.protocol import DEFAULT_TUNING, TransportTuning, make_config
from grpclib_transports.workers import WorkerHost

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path
    from ssl import SSLContext

    from grpclib._typing import IServable
    from grpclib.encoding.base import CodecBase, StatusDetailsCodecBase


class Endpoint:
    """A virtual endpoint: services plus transport-specific bindings."""

    def __init__(
        self,
        app: Server,
        handlers: Collection[IServable],
        *,
        codec: CodecBase | None = None,
        status_details_codec: StatusDetailsCodecBase | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        self.app = app
        self.handlers = limit_services_concurrency(
            handlers,
            max_concurrency=max_concurrency,
        )
        self.codec = codec
        self.status_details_codec = status_details_codec

    def _make_server(self) -> GrpclibServer:
        return GrpclibServer(
            self.handlers,
            codec=self.codec,
            status_details_codec=self.status_details_codec,
            config=make_config(self.app.tuning),
        )

    async def listen(
        self,
        host: str | None = None,
        port: int | None = None,
        *,
        path: str | Path | None = None,
        family: socket.AddressFamily = socket.AF_UNSPEC,
        flags: socket.AddressInfo = socket.AI_PASSIVE,
        sock: socket.socket | None = None,
        backlog: int = 100,
        ssl: SSLContext | None = None,
        reuse_address: bool | None = None,
        reuse_port: bool | None = None,
    ) -> GrpclibServer:
        server = self._make_server()
        await server.start(
            host=host,
            port=port,
            path=str(path) if path is not None else None,
            family=family,
            flags=flags,
            sock=sock,
            backlog=backlog,
            ssl=ssl,
            reuse_address=reuse_address,
            reuse_port=reuse_port,
        )
        self.app.track_server(server)
        return server

    async def listen_unix(
        self,
        path: str | Path,
        *,
        backlog: int = 100,
    ) -> GrpclibServer:
        return await self.listen(path=path, backlog=backlog)

    async def listen_tcp(
        self,
        host: str,
        port: int,
        *,
        family: socket.AddressFamily = socket.AF_UNSPEC,
        flags: socket.AddressInfo = socket.AI_PASSIVE,
        backlog: int = 100,
        ssl: SSLContext | None = None,
        reuse_address: bool | None = None,
        reuse_port: bool | None = None,
    ) -> GrpclibServer:
        return await self.listen(
            host=host,
            port=port,
            family=family,
            flags=flags,
            backlog=backlog,
            ssl=ssl,
            reuse_address=reuse_address,
            reuse_port=reuse_port,
        )

    def for_workers(self) -> WorkerHost:
        return WorkerHost(self.handlers, tuning=self.app.tuning)


class Server:
    """Container for multiple service endpoints and worker managers."""

    def __init__(
        self,
        *,
        tuning: TransportTuning = DEFAULT_TUNING,
    ) -> None:
        self.tuning = tuning
        self._servers: list[GrpclibServer] = []

    def endpoint(
        self,
        handlers: Collection[IServable],
        *,
        codec: CodecBase | None = None,
        status_details_codec: StatusDetailsCodecBase | None = None,
        max_concurrency: int | None = None,
    ) -> Endpoint:
        return Endpoint(
            self,
            handlers,
            codec=codec,
            status_details_codec=status_details_codec,
            max_concurrency=max_concurrency,
        )

    def track_server(self, server: GrpclibServer) -> None:
        self._servers.append(server)

    def close(self) -> None:
        for server in self._servers:
            server.close()

    async def wait_closed(self) -> None:
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()

    async def __aenter__(self) -> Server:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        self.close()
        await self.wait_closed()
