"""HTTP binary cache server for the local Nix store.

Serves the standard Nix binary cache protocol over HTTP/HTTPS.
Metadata queries (narinfo) are served from LocalStoreDB when available,
falling back to the daemon protocol. NAR data is always streamed from
the daemon via NarFromPath.

Endpoints:
    GET /nix-cache-info       → cache metadata
    GET /{hash}.narinfo       → path metadata
    GET /nar/{hash}.nar       → NAR archive data
    PUT /nar/{hash}.nar       → upload NAR data (temporary)
    PUT /{hash}.narinfo       → upload path metadata (finalizes upload)

Supports optional basic auth and TLS.
"""

from __future__ import annotations

import base64
import os
import ssl
from http import HTTPStatus
from pathlib import Path

import structlog
from aiohttp import web
from passlib.apache import HtpasswdFile

from .local_store_db import LocalStoreDB
from .operations.add_to_store_nar import AddToStoreNarRequest
from .operations.nar_from_path import NarFromPathRequest
from .operations.query_path_from_hash_part import QueryPathFromHashPartRequest
from .operations.query_path_info import QueryPathInfoRequest
from .operations.base import ValidPathInfo
from .store import Store
from .store_path import StorePath
from .wire import NixWriter

log = structlog.get_logger(__name__)

_STORE_PREFIX = "/nix/store/"


def strip_store_prefix(path: StorePath | str) -> str:
    """'/nix/store/abc-foo' → 'abc-foo'"""
    path_str = str(path)
    if path_str.startswith(_STORE_PREFIX):
        return path_str[len(_STORE_PREFIX) :]
    return path_str


class BinaryCacheServer:
    """HTTP binary cache server for the local Nix store.

    Supports reading and (optionally) writing paths via standard protocol.
    """

    def __init__(
        self,
        local_store: Store,
        *,
        username: str | None = None,
        password: str | None = None,
        htpasswd_path: str | Path | None = None,
        priority: int = 30,
        upload_dir: str | Path | None = None,
    ) -> None:
        self.store = local_store
        self.username = username
        self.password = password
        self.htpasswd = None
        if htpasswd_path:
            self.htpasswd = HtpasswdFile(str(htpasswd_path))
        self.priority = priority
        self.upload_dir = Path(upload_dir) if upload_dir else None
        if self.upload_dir:
            self.upload_dir.mkdir(parents=True, exist_ok=True)

        self.app = web.Application(
            middlewares=[self.auth_middleware],
            client_max_size=1024**4,  # 1 TiB
        )
        self.app.router.add_get("/nix-cache-info", self.handle_cache_info)
        self.app.router.add_get("/{hash}.narinfo", self.handle_narinfo)
        self.app.router.add_get("/nar/{filename:.+}", self.handle_nar)

        if self.upload_dir:
            self.app.router.add_put("/nar/{filename:.+}", self.handle_put_nar)
            self.app.router.add_put("/{hash}.narinfo", self.handle_put_narinfo)

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
        if self.username is None and self.htpasswd is None:
            return await handler(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return web.Response(
                status=HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": 'Basic realm="nix-cache"'},
                text="Authentication required\n",
            )

        try:
            auth_decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            user, passwd = auth_decoded.split(":", 1)
        except Exception:
            return web.Response(
                status=HTTPStatus.UNAUTHORIZED, text="Malformed credentials\n"
            )

        if self.htpasswd:
            if not self.htpasswd.check_password(user, passwd):
                return web.Response(
                    status=HTTPStatus.FORBIDDEN, text="Invalid credentials\n"
                )
        elif self.username is not None:
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
        vinfo = await self.get_path_info(path)
        if vinfo is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        narinfo = vinfo.to_narinfo()

        if self.db is not None:
            self.db.mark_path(path)

        return web.Response(
            text=narinfo,
            content_type="text/x-nix-narinfo",
        )

    async def handle_nar(self, request: web.Request) -> web.StreamResponse:
        filename = request.match_info["filename"]
        # Standard format is /nar/<hash>.nar[.comp]
        hash_part = filename.split(".", 1)[0]

        if not filename.endswith(".nar"):
            # We only serve raw NARs. If Nix asks for .xz or other, we return 404
            # so it might fall back to .nar if it wants.
            return web.Response(
                status=HTTPStatus.NOT_FOUND, text="compression not supported\n"
            )

        path = await self.resolve_path(hash_part)
        if path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        # Get path info for NAR size (needed for Content-Length and streaming)
        vinfo = await self.get_path_info(path)
        if vinfo is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="not found\n")

        response = web.StreamResponse(
            status=HTTPStatus.OK,
            headers={
                "Content-Type": "application/x-nix-nar",
                "Content-Length": str(vinfo.nar_size),
            },
        )
        await response.prepare(request)

        try:
            await self.store.execute(
                NarFromPathRequest(path=path),
                client=NixWriter(response),  # type: ignore[arg-type]
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

    async def handle_put_nar(self, request: web.Request) -> web.Response:
        """Receive NAR data and save it to a temporary file."""
        if not self.upload_dir:
            return web.Response(
                status=HTTPStatus.METHOD_NOT_ALLOWED, text="Upload disabled\n"
            )

        filename = request.match_info["filename"]
        # Standard format is /nar/<hash>.nar[.comp]
        hash_part = filename.split(".", 1)[0]
        temp_path = self.upload_dir / f"{hash_part}.nar"

        if ".nar" not in filename:
            log.warning("invalid_nar_upload_name", filename=filename)
            return web.Response(
                status=HTTPStatus.BAD_REQUEST, text="Filename must contain .nar\n"
            )

        log.info("receiving_nar_upload", hash=hash_part, path=str(temp_path))
        with open(temp_path, "wb") as f:
            async for chunk in request.content.iter_any():
                f.write(chunk)

        log.info("nar_upload_complete", hash=hash_part, size=temp_path.stat().st_size)
        return web.Response(status=HTTPStatus.OK, text="ok\n")

    async def handle_put_narinfo(self, request: web.Request) -> web.Response:
        """Receive .narinfo, parse it, and finalize the upload to the Nix store."""
        if not self.upload_dir:
            return web.Response(
                status=HTTPStatus.METHOD_NOT_ALLOWED, text="Upload disabled\n"
            )

        content = await request.text()

        try:
            vinfo = ValidPathInfo.from_narinfo(content)
        except Exception as e:
            log.warning("invalid_narinfo_upload", error=str(e))
            return web.Response(
                status=HTTPStatus.BAD_REQUEST, text=f"Invalid .narinfo: {e}\n"
            )

        # Determine the NAR filename from the 'URL' field in .narinfo
        nar_url = ""
        for line in content.splitlines():
            if line.startswith("URL: "):
                nar_url = line.split(": ", 1)[1].strip()
                break

        if not nar_url:
            return web.Response(
                status=HTTPStatus.BAD_REQUEST, text="Missing 'URL' field in .narinfo\n"
            )

        # nar_url is usually "nar/<narhash>.nar[.comp]"
        nar_filename = nar_url.split("/")[-1]
        nar_hash_part = nar_filename.split(".", 1)[0]

        # Check if the NAR exists
        nar_temp_path = self.upload_dir / f"{nar_hash_part}.nar"
        if not nar_temp_path.exists():
            log.warning(
                "nar_missing_for_narinfo", nar_hash=nar_hash_part, path=vinfo.path
            )
            return web.Response(
                status=HTTPStatus.NOT_FOUND,
                text=f"NAR {nar_hash_part} not found. Upload it first.\n",
            )

        # Now add it to the store
        async def provide_nar(writer: NixWriter):
            # We need to wrap the raw NAR in Nix framing (framed NAR data)
            framed = writer.framed()
            with open(nar_temp_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    framed.write(chunk)
            await framed.finalize()

        log.info("finalizing_upload_to_store", path=vinfo.path)
        try:
            req = AddToStoreNarRequest(info=vinfo, async_provider=provide_nar)
            await self.store.execute(req)
        except Exception as e:
            log.exception("finalize_upload_failed", path=vinfo.path)
            return web.Response(
                status=HTTPStatus.INTERNAL_SERVER_ERROR, text=f"Finalize failed: {e}\n"
            )
        finally:
            # Clean up temporary NAR
            try:
                os.remove(nar_temp_path)
            except OSError:
                pass

        log.info("upload_to_store_complete", path=vinfo.path)
        return web.Response(status=HTTPStatus.OK, text="ok\n")

    # ── Path resolution helpers ───────────────────────────────────────

    async def resolve_path(self, hash_part: str) -> StorePath | None:
        """Resolve a store hash to a full store path."""
        resp = await self.store.execute(
            QueryPathFromHashPartRequest(path=StorePath(hash_part))
        )
        return StorePath(resp.value) if resp.value else None

    async def get_path_info(self, path: StorePath) -> ValidPathInfo | None:
        """Get ValidPathInfo for a store path. Returns ValidPathInfo or None."""
        resp = await self.store.execute(QueryPathInfoRequest(path=path))
        if resp.valid and resp.info:
            return resp.info.with_path(path)
        return None

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
