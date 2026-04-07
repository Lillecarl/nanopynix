"""HTTP binary cache server for the local Nix store.

Serves the standard Nix binary cache protocol (read-only) over HTTP/HTTPS.
Metadata queries (narinfo) are served from LocalStoreDB when available,
falling back to the daemon protocol. NAR data is always streamed from
the daemon via NarFromPath.

Endpoints:
    GET /nix-cache-info       → cache metadata
    GET /{hash}.narinfo       → path metadata
    GET /nar/{hash}.nar       → NAR archive data

Supports optional basic auth and TLS.
"""

from __future__ import annotations

import base64
import ssl
from http import HTTPStatus

import structlog
from aiohttp import web

from .local_store_db import LocalStoreDB
from .operations.query_path_from_hash_part import QueryPathFromHashPartRequest
from .operations.query_path_info import QueryPathInfoRequest
from .store import Store
from .store_path import StorePath

log = structlog.get_logger(__name__)

_STORE_PREFIX = "/nix/store/"


def strip_store_prefix(path: StorePath | str) -> str:
    """'/nix/store/abc-foo' → 'abc-foo'"""
    path_str = str(path)
    if path_str.startswith(_STORE_PREFIX):
        return path_str[len(_STORE_PREFIX) :]
    return path_str


def hash_part(path: StorePath | str) -> str:
    """'/nix/store/abc-foo' → 'abc'"""
    return strip_store_prefix(path).split("-", 1)[0]


def format_narinfo(
    path: StorePath,
    nar_hash: str,
    nar_size: int,
    references: set[StorePath],
    deriver: StorePath,
    sigs: set[str],
    ca: str,
) -> str:
    """Format a .narinfo file from path info fields."""
    store_hash = hash_part(path)
    # Ensure NarHash has sha256: prefix (expected by Nix)
    if not nar_hash.startswith("sha256:"):
        nar_hash = f"sha256:{nar_hash}"

    lines = [
        f"StorePath: {path}",
        f"URL: nar/{store_hash}.nar",
        "Compression: none",
        f"NarHash: {nar_hash}",
        f"NarSize: {nar_size}",
    ]

    if references:
        refs = " ".join(sorted(strip_store_prefix(r) for r in references))
        lines.append(f"References: {refs}")

    if deriver:
        lines.append(f"Deriver: {strip_store_prefix(deriver)}")

    for sig in sorted(sigs):
        lines.append(f"Sig: {sig}")

    if ca:
        lines.append(f"CA: {ca}")

    return "\n".join(lines) + "\n"


class BinaryCacheServer:
    """Read-only HTTP binary cache backed by a local Nix store."""

    def __init__(
        self,
        local_store: Store,
        *,
        username: str | None = None,
        password: str | None = None,
        priority: int = 30,
    ) -> None:
        self.store = local_store
        self.username = username
        self.password = password
        self.priority = priority

        self.app = web.Application(middlewares=[self.auth_middleware])
        self.app.router.add_get("/nix-cache-info", self.handle_cache_info)
        self.app.router.add_get("/{hash}.narinfo", self.handle_narinfo)
        self.app.router.add_get("/nar/{hash}.nar", self.handle_nar)

    @property
    def db(self) -> LocalStoreDB | None:
        return self.store.db

    # ── Auth middleware ────────────────────────────────────────────────

    @web.middleware
    async def auth_middleware(
        self,
        request: web.Request,
        handler,
    ) -> web.StreamResponse:
        if self.username is None:
            return await handler(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return web.Response(
                status=HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": 'Basic realm="nix-cache"'},
                text="Authentication required\n",
            )

        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            user, passwd = decoded.split(":", 1)
        except Exception:
            return web.Response(
                status=HTTPStatus.UNAUTHORIZED, text="Malformed credentials\n"
            )

        if user != self.username or passwd != self.password:
            return web.Response(
                status=HTTPStatus.FORBIDDEN, text="Invalid credentials\n"
            )

        return await handler(request)

    # ── Handlers ──────────────────────────────────────────────────────

    async def handle_cache_info(self, request: web.Request) -> web.Response:
        lines = [
            "StoreDir: /nix/store",
            "WantMassQuery: 1",
            f"Priority: {self.priority}",
        ]
        return web.Response(text="\n".join(lines) + "\n")

    async def handle_narinfo(self, request: web.Request) -> web.Response:
        hash_part = request.match_info["hash"]

        # Resolve hash → full path
        path = await self.resolve_path(hash_part)
        if path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        # Get path info
        info = await self.get_path_info(path)
        if info is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        narinfo = format_narinfo(
            path=info.path,
            nar_hash=info.nar_hash,
            nar_size=info.nar_size,
            references=info.references,
            deriver=info.deriver,
            sigs=info.sigs,
            ca=info.ca,
        )

        if self.db is not None:
            self.db.mark_path(path)

        return web.Response(
            text=narinfo,
            content_type="text/x-nix-narinfo",
        )

    async def handle_nar(self, request: web.Request) -> web.StreamResponse:
        hash_part = request.match_info["hash"]

        path = await self.resolve_path(hash_part)
        if path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        # Get path info for NAR size (needed for Content-Length and streaming)
        info = await self.get_path_info(path)
        if info is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        response = web.StreamResponse(
            status=HTTPStatus.OK,
            headers={
                "Content-Type": "application/x-nix-nar",
                "Content-Length": str(info.nar_size),
            },
        )
        await response.prepare(request)

        try:
            from .operations.nar_from_path import NarFromPathRequest

            await self.store.execute(
                NarFromPathRequest(
                    path=path,
                    nar_size=info.nar_size,
                    async_callback=response.write,
                )
            )
        except Exception:
            log.exception("nar_from_path_streaming_failed", path=path)
            # Response already started — can't change status code.
            # We close the connection abruptly to signal failure.
            # aiohttp: force-close the underlying transport.
            if request.protocol and request.protocol.transport:
                request.protocol.transport.close()
            raise

        if self.db is not None:
            self.db.mark_path(path)

        await response.write_eof()
        return response

    # ── Path resolution helpers ───────────────────────────────────────

    async def resolve_path(self, hash_part: str) -> StorePath | None:
        """Resolve a store hash to a full store path."""
        resp = await self.store.execute(
            QueryPathFromHashPartRequest(path=StorePath(hash_part))
        )
        return StorePath(resp.value) if resp.value else None

    async def get_path_info(self, path: StorePath):
        """Get PathInfo for a store path. Returns PathInfo or None."""
        resp = await self.store.execute(QueryPathInfoRequest(path=path))
        return resp.info if resp.valid else None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        ssl_cert: str | None = None,
        ssl_key: str | None = None,
    ) -> tuple[web.AppRunner, int]:
        """Start the HTTP server. Returns (runner, bound_port)."""
        ssl_ctx = None
        if ssl_cert and ssl_key:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(ssl_cert, ssl_key)
            scheme = "https"
        else:
            scheme = "http"

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port, ssl_context=ssl_ctx)
        await site.start()

        # Resolve the actual bound port (important when port=0)
        # types-aiohttp don't declare sockets on AbstractServer (added in Python 3.7)
        bound_port = site._server.sockets[0].getsockname()[1]  # type: ignore[reportAttributeAccessIssue]
        log.info(
            "binary_cache_server_listening",
            scheme=scheme,
            host=host,
            port=bound_port,
        )
        return runner, bound_port
