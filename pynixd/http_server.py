"""Unified HTTP server for pynixd.

Provides:
- Nix binary cache protocol (narinfo, NAR streaming, uploads)
- Prometheus metrics endpoint (/metrics)

Supports granular enabling of features (cache vs metrics) and
optional basic auth/TLS.
"""

from __future__ import annotations

import asyncio
import base64
import bz2
import contextlib
import gzip
import lzma
import ssl
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import brotli
import lz4.frame
import structlog
import zstandard as zstd
from aiohttp import web
from passlib.apache import HtpasswdFile

from . import metrics
from .operations.add_to_store_nar import AddToStoreNarRequest
from .operations.base import ValidPathInfo
from .operations.nar_from_path import NarFromPathRequest
from .operations.query_path_from_hash_part import QueryPathFromHashPartRequest
from .operations.query_path_info import QueryPathInfoRequest
from .store_path import StorePath

if TYPE_CHECKING:
    from .local_store_db import LocalStoreDB
    from .store import Store
    from .wire import NixWriter

log = structlog.get_logger(__name__)


class PynixdHttpServer:
    """Unified HTTP server for pynixd.

    Supports reading and (optionally) writing paths via standard protocol,
    and exposing Prometheus metrics.
    """

    def __init__(
        self,
        local_store: Store,
        *,
        enable_cache: bool = True,
        enable_metrics: bool = True,
        metrics_no_auth: bool = True,
        username: str | None = None,
        password: str | None = None,
        htpasswd_path: str | Path | None = None,
        priority: int = 30,
        upload_dir: str | Path | None = None,
    ) -> None:
        self.store = local_store
        self.enable_cache = enable_cache
        self.enable_metrics = enable_metrics
        self.metrics_no_auth = metrics_no_auth
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

        if self.enable_metrics:
            self.app.router.add_get("/metrics", self.handle_metrics)

        if self.enable_cache:
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
        handler: Any,
    ) -> web.StreamResponse:
        # Check if we should skip auth for metrics
        if self.enable_metrics and self.metrics_no_auth and request.path == "/metrics":
            return await handler(request)

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
                status=HTTPStatus.UNAUTHORIZED,
                text="Malformed credentials\n",
            )

        if self.htpasswd:
            if not self.htpasswd.check_password(user, passwd):
                return web.Response(
                    status=HTTPStatus.FORBIDDEN,
                    text="Invalid credentials\n",
                )
        elif self.username is not None and (user != self.username or passwd != self.password):
            return web.Response(
                status=HTTPStatus.FORBIDDEN,
                text="Invalid credentials\n",
            )

        return await handler(request)

    # ── Handlers ──────────────────────────────────────────────────────

    async def handle_metrics(self, request: web.Request) -> web.Response:
        """Expose Prometheus metrics."""
        body, content_type = metrics.get_metrics_response()
        # aiohttp: charset must not be in content_type argument
        content_type_only = content_type.split(";")[0]
        return web.Response(body=body, content_type=content_type_only)

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
                status=HTTPStatus.NOT_FOUND,
                text="compression not supported\n",
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

        async def provide_nar(chunk: bytes):
            await response.write(chunk)

        try:
            await self.store.execute(
                NarFromPathRequest(
                    path=path,
                    nar_size=vinfo.nar_size,
                    async_callback=provide_nar,
                ),
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
                status=HTTPStatus.METHOD_NOT_ALLOWED,
                text="Upload disabled\n",
            )

        filename = request.match_info["filename"]
        # Standard format is /nar/<hash>.nar[.comp]
        # Strip all extensions to get the hash part
        hash_part = filename.split(".", 1)[0]

        # Determine compression from extension
        ext = ""
        for known_ext in [".xz", ".bz2", ".gz", ".zst", ".lz4", ".br"]:
            if filename.endswith(known_ext):
                ext = known_ext
                break

        temp_path = self.upload_dir / f"{hash_part}.nar{ext}"

        if ".nar" not in filename:
            log.warning("invalid_nar_upload_name", filename=filename)
            return web.Response(
                status=HTTPStatus.BAD_REQUEST,
                text="Filename must contain .nar\n",
            )

        log.info(
            "receiving_nar_upload",
            hash=hash_part,
            path=str(temp_path),
            filename=filename,
        )
        # Use anyio.open_file for async file writing to keep event loop non-blocking
        async with await anyio.open_file(temp_path, "wb") as f:
            async for chunk in request.content.iter_any():
                await f.write(chunk)

        nar_size = (await anyio.Path(temp_path).stat()).st_size
        log.info("nar_upload_complete", hash=hash_part, size=nar_size)
        return web.Response(status=HTTPStatus.OK, text="ok\n")

    async def handle_put_narinfo(self, request: web.Request) -> web.Response:
        """Receive .narinfo, parse it, and finalize the upload to the Nix store."""
        if not self.upload_dir:
            return web.Response(
                status=HTTPStatus.METHOD_NOT_ALLOWED,
                text="Upload disabled\n",
            )

        content = await request.text()

        try:
            vinfo = ValidPathInfo.from_narinfo(content)
        except Exception as e:
            log.warning("invalid_narinfo_upload", error=str(e))
            return web.Response(
                status=HTTPStatus.BAD_REQUEST,
                text=f"Invalid .narinfo: {e}\n",
            )

        # Determine the NAR filename from the 'URL' field in .narinfo
        nar_url = ""
        for line in content.splitlines():
            if line.startswith("URL: "):
                nar_url = line.split(": ", 1)[1].strip()
                break

        if not nar_url:
            return web.Response(
                status=HTTPStatus.BAD_REQUEST,
                text="Missing 'URL' field in .narinfo\n",
            )

        # nar_url is usually "nar/<narhash>.nar[.comp]"
        nar_filename = nar_url.split("/")[-1]
        nar_hash_part = nar_filename.split(".", 1)[0]

        # Check for any compressed variant
        nar_temp_path = None
        for ext in ["", ".xz", ".bz2", ".gz", ".zst", ".lz4", ".br"]:
            p = self.upload_dir / f"{nar_hash_part}.nar{ext}"
            if p.exists():
                nar_temp_path = p
                break

        if not nar_temp_path:
            log.warning(
                "nar_missing_for_narinfo",
                nar_hash=nar_hash_part,
                path=vinfo.path,
            )
            return web.Response(
                status=HTTPStatus.NOT_FOUND,
                text=f"NAR {nar_hash_part} not found. Upload it first.\n",
            )

        # Now add it to the store
        async def provide_nar(writer: NixWriter):
            log.debug("provide_nar_start", path=nar_temp_path)
            # We need to wrap the raw NAR in Nix framing (framed NAR data)
            framed = writer.framed()
            loop = asyncio.get_running_loop()

            def decompress_gen(path: Path):
                if path.name.endswith(".xz"):
                    ctx = lzma.open(path, "rb")
                elif path.name.endswith(".bz2"):
                    ctx = bz2.open(path, "rb")
                elif path.name.endswith(".gz"):
                    ctx = gzip.open(path, "rb")
                elif path.name.endswith(".zst"):
                    dctx = zstd.ZstdDecompressor()
                    ctx = dctx.stream_reader(path.open("rb"))
                elif path.name.endswith(".lz4"):
                    ctx = lz4.frame.open(path, "rb")
                elif path.name.endswith(".br"):
                    d = brotli.Decompressor()
                    with path.open("rb") as bf:
                        while True:
                            chunk = bf.read(1024 * 1024)
                            if not chunk:
                                break
                            yield d.process(chunk)
                    return
                else:
                    ctx = path.open("rb")

                with ctx as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk

            gen = decompress_gen(nar_temp_path)

            def get_next_chunk(g):
                try:
                    return next(g)
                except StopIteration:
                    return None

            sent_bytes = 0
            while True:
                # Get next chunk from generator in a thread to keep event loop free
                chunk = await loop.run_in_executor(None, get_next_chunk, gen)
                if chunk is None:
                    break

                # Ensure chunk is bytes for type safety
                if not isinstance(chunk, bytes):
                    raise TypeError(f"Expected bytes, got {type(chunk)}")

                framed.write(chunk)
                sent_bytes += len(chunk)
                log.debug("provide_nar_progress", sent_bytes=sent_bytes)

            log.debug("provide_nar_finalizing", total_bytes=sent_bytes)
            await framed.finalize()
            log.debug("provide_nar_done")

        log.info("finalizing_upload_to_store", path=vinfo.path)
        try:
            req = AddToStoreNarRequest(info=vinfo, async_provider=provide_nar)
            await self.store.execute(req)
        except Exception as e:
            log.exception("finalize_upload_failed", path=vinfo.path)
            return web.Response(
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                text=f"Finalize failed: {e}\n",
            )
        finally:
            # Clean up temporary NAR
            with contextlib.suppress(OSError):
                nar_temp_path.unlink()

        self.store.add_path_info(vinfo)
        self.store.tracker.add_known_path(vinfo.path)
        log.info("upload_to_store_complete", path=vinfo.path)
        return web.Response(status=HTTPStatus.OK, text="ok\n")

    # ── Path resolution helpers ───────────────────────────────────────

    async def resolve_path(self, hash_part: str) -> StorePath | None:
        """Resolve a store hash or NAR hash to a full store path."""
        # 1. Try resolving as a NAR hash (SHA256, 64 chars) if we have a DB
        if len(hash_part) == 64 and self.db is not None:
            # Nix stores NAR hashes as 'sha256:...' in the DB
            # but sometimes they are stored without the prefix or with a different one.
            # We try both.
            for prefix in ["sha256:", ""]:
                full_hash = f"{prefix}{hash_part}"
                async with self.db.execute(
                    "SELECT path FROM ValidPaths WHERE hash = ?",
                    (full_hash,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    return StorePath(row[0])

        # 2. Fall back to standard QueryPathFromHashPart (for 32-char store path hashes)
        resp = await self.store.execute(
            QueryPathFromHashPartRequest(path=StorePath(hash_part)),
        )
        if resp.value:
            return StorePath(resp.value)

        return None

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
            "pynixd_http_server_listening",
            scheme=scheme,
            host=host,
            port=bound_port,
            enable_cache=self.enable_cache,
            enable_metrics=self.enable_metrics,
        )
        return runner, bound_port


# Backward compatibility alias
BinaryCacheServer = PynixdHttpServer
