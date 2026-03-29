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
import logging
import ssl
from http import HTTPStatus

from aiohttp import web

from .local_store_db import LocalStoreDB
from .operations.queries import QueryPathFromHashPartRequest
from .store import Store

log = logging.getLogger(__name__)

_STORE_PREFIX = "/nix/store/"


def _strip_store_prefix(path: str) -> str:
    """'/nix/store/abc-foo' → 'abc-foo'"""
    if path.startswith(_STORE_PREFIX):
        return path[len(_STORE_PREFIX) :]
    return path


def _hash_part(path: str) -> str:
    """'/nix/store/abc-foo' → 'abc'"""
    return _strip_store_prefix(path).split("-", 1)[0]


def _format_narinfo(
    path: str,
    nar_hash: str,
    nar_size: int,
    references: set[str],
    deriver: str,
    sigs: set[str],
    ca: str,
) -> str:
    """Format a .narinfo file from path info fields."""
    store_hash = _hash_part(path)
    lines = [
        f"StorePath: {path}",
        f"URL: nar/{store_hash}.nar",
        "Compression: none",
        f"NarHash: {nar_hash}",
        f"NarSize: {nar_size}",
    ]

    if references:
        refs = " ".join(sorted(_strip_store_prefix(r) for r in references))
        lines.append(f"References: {refs}")

    if deriver:
        lines.append(f"Deriver: {_strip_store_prefix(deriver)}")

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
        self._store = local_store
        self._username = username
        self._password = password
        self._priority = priority

        self._app = web.Application(middlewares=[self._auth_middleware])
        self._app.router.add_get("/nix-cache-info", self._handle_cache_info)
        self._app.router.add_get("/{hash}.narinfo", self._handle_narinfo)
        self._app.router.add_get("/nar/{hash}.nar", self._handle_nar)

    @property
    def db(self) -> LocalStoreDB | None:
        return self._store.db

    # ── Auth middleware ────────────────────────────────────────────────

    @web.middleware
    async def _auth_middleware(
        self,
        request: web.Request,
        handler,
    ) -> web.StreamResponse:
        if self._username is None:
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

        if user != self._username or passwd != self._password:
            return web.Response(
                status=HTTPStatus.FORBIDDEN, text="Invalid credentials\n"
            )

        return await handler(request)

    # ── Handlers ──────────────────────────────────────────────────────

    async def _handle_cache_info(self, request: web.Request) -> web.Response:
        lines = [
            "StoreDir: /nix/store",
            "WantMassQuery: 1",
            f"Priority: {self._priority}",
        ]
        return web.Response(text="\n".join(lines) + "\n")

    async def _handle_narinfo(self, request: web.Request) -> web.Response:
        hash_part = request.match_info["hash"]

        # Resolve hash → full path
        path = await self._resolve_path(hash_part)
        if path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        # Get path info
        info = await self._get_path_info(path)
        if info is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        narinfo = _format_narinfo(
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

    async def _handle_nar(self, request: web.Request) -> web.StreamResponse:
        hash_part = request.match_info["hash"]

        path = await self._resolve_path(hash_part)
        if path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        # Get path info for NAR size (needed for Content-Length and streaming)
        info = await self._get_path_info(path)
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
            await self._store.nar_from_path_chunked(
                path,
                info.nar_size,
                response.write,
            )
        except Exception:
            log.exception("nar_from_path streaming failed for %s", path)
            # Response already started — can't change status code.
            # Client will see a truncated response.
            return response

        if self.db is not None:
            self.db.mark_path(path)

        await response.write_eof()
        return response

    # ── Path resolution helpers ───────────────────────────────────────

    async def _resolve_path(self, hash_part: str) -> str | None:
        """Resolve a store hash to a full store path."""
        # Try SQLite first
        if self.db is not None:
            path = await self.db.query_path_from_hash_part(hash_part)
            if path is not None:
                return path

        # Fallback: query daemon
        try:
            async with self._store.transfer_conn() as conn:
                resp = await conn.call(
                    QueryPathFromHashPartRequest(path=hash_part),
                )
                return resp.value if resp.value else None
        except Exception:
            log.debug(
                "query_path_from_hash_part daemon fallback failed for %s", hash_part
            )
            return None

    async def _get_path_info(self, path: str):
        """Get PathInfo for a store path. Returns PathInfo or None."""
        return await self._store.query_path_info(path)

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

        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, host, port, ssl_context=ssl_ctx)
        await site.start()

        # Resolve the actual bound port (important when port=0)
        # types-aiohttp don't declare sockets on AbstractServer (added in Python 3.7)
        bound_port = site._server.sockets[0].getsockname()[1]  # type: ignore[reportAttributeAccessIssue]
        log.info(
            "Binary cache server listening on %s://%s:%d", scheme, host, bound_port
        )
        return runner, bound_port
