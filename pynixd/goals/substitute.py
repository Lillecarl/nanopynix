"""Substitution goals for HTTP binary cache paths."""

from __future__ import annotations

import bz2
import hashlib
import lzma
import re
import time
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import aiohttp
import structlog
import zstandard as zstd

from ..serde import AddToStoreNarRequest, BuildResultStatus, IsValidPathRequest
from ..serde.content_address import ContentAddress
from ..serde.context import ReadContext, WriteContext
from ..serde.nar_hash import NARHash
from ..serde.path_info import UnkeyedValidPathInfo
from ..serde.signature import Signature
from ..serde.store_path import StorePath as SerdeStorePath
from ..serde.valid_path_info import ValidPathInfo
from ..serde.wire_time import Time
from ..store_path import StorePath
from .goal import ExecutionGoal
from .results import GoalResult, goal_failure, goal_success

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from .engine import GoalEngine

log = structlog.get_logger(__name__)

_NARINFO_KEY_RE = re.compile(r"^([A-Za-z]+):\s*(.*)$")
_DEFAULT_SUBSTITUTERS = ("https://cache.nixos.org/",)
_HTTP_CHUNK_SIZE = 1024 * 256


@dataclass(frozen=True)
class NarInfo:
    path: StorePath
    url: str
    compression: str
    file_hash: str
    file_size: int
    valid_path_info: ValidPathInfo

    @property
    def references(self) -> set[StorePath]:
        return {StorePath(str(path)) for path in self.valid_path_info.info.references}


@dataclass(frozen=True)
class SubstituteAttempt:
    found: bool
    result: GoalResult


@dataclass(frozen=True)
class _CacheHit:
    base_url: str
    narinfo: NarInfo

    @property
    def nar_url(self) -> str:
        return urljoin(self.base_url, self.narinfo.url)


class HTTPBinaryCacheClient:
    """Small Nix HTTP binary cache client owned by the goal engine."""

    def __init__(self, base_urls: Iterable[str]) -> None:
        self._base_urls = [normalised for url in base_urls if (normalised := _normalise_base_url(url)) is not None]

    async def find_path(self, path: StorePath) -> _CacheHit | None:
        if not self._base_urls:
            return None

        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=50, force_close=False),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            for base_url in self._base_urls:
                hit = await self._try_cache(session, base_url, path)
                if hit is not None:
                    return hit
        return None

    async def stream_nar(self, hit: _CacheHit) -> AsyncIterator[bytes]:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=10, force_close=False),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60),
        ) as session:
            async with session.get(hit.nar_url, raise_for_status=False) as response:
                if response.status != 200:
                    raise RuntimeError(f"substituter returned HTTP {response.status} for {hit.nar_url}")
                decompressor = _decompressor(hit.narinfo.compression)
                async for chunk in response.content.iter_chunked(_HTTP_CHUNK_SIZE):
                    for out in decompressor.decompress(chunk):
                        if out:
                            yield out
                for out in decompressor.finish():
                    if out:
                        yield out

    async def _try_cache(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        path: StorePath,
    ) -> _CacheHit | None:
        url = urljoin(base_url, f"{path.hash_part()}.narinfo")
        try:
            async with session.get(url, raise_for_status=False) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    log.debug("substituter_narinfo_http_status", url=url, status=response.status)
                    return None
                text = await response.text()
        except (TimeoutError, aiohttp.ClientError, OSError):
            log.debug("substituter_narinfo_failed", url=url, exc_info=True)
            return None

        narinfo = _parse_narinfo(text, expected_path=path)
        if narinfo is None:
            log.debug("substituter_narinfo_parse_failed", url=url, path=str(path))
            return None
        return _CacheHit(base_url=base_url, narinfo=narinfo)


class SubstitutePathGoal(ExecutionGoal[SubstituteAttempt]):
    """Substitute one store path and its reference closure."""

    def __init__(self, engine: GoalEngine, path: StorePath, substituter_urls: tuple[str, ...]) -> None:
        super().__init__(engine)
        self.path = path
        self.substituter_urls = substituter_urls

    async def _run(self) -> SubstituteAttempt:
        log.debug("substitute_path_start", path=str(self.path), substituters=self.substituter_urls)
        if await self._is_valid_local_path(self.path):
            log.debug("substitute_path_already_valid", path=str(self.path))
            result = goal_success()
            result.produced_paths.add(self.path)
            return SubstituteAttempt(found=True, result=result)

        client = HTTPBinaryCacheClient(self.substituter_urls)
        hit = await client.find_path(self.path)
        if hit is None:
            log.debug("substitute_path_miss", path=str(self.path))
            return SubstituteAttempt(
                found=False,
                result=goal_failure(
                    f"pynixd: no substituter has path: {self.path}",
                    BuildResultStatus.UNKNOWN,
                ),
            )

        reference_goals: list[SubstitutePathGoal] = []
        for reference in sorted(hit.narinfo.references, key=str):
            if reference == self.path:
                continue
            reference_goals.append(await self.engine.get_substitute_path_goal(reference, self.substituter_urls))
        log.debug("substitute_path_hit", path=str(self.path), references=len(reference_goals))

        reference_results = await self.run_children(reference_goals)
        for reference_result in reference_results:
            if not reference_result.found:
                return SubstituteAttempt(
                    found=True,
                    result=goal_failure(
                        f"pynixd: cannot substitute {self.path}; missing reference",
                        BuildResultStatus.UNKNOWN,
                    ),
                )
            if not _result_succeeded(reference_result.result):
                return SubstituteAttempt(found=True, result=reference_result.result)

        try:
            log.debug("substitute_path_import_start", path=str(self.path), nar_url=hit.nar_url)
            await self._import_nar(client, hit)
        except Exception as exc:
            log.warning("substitute_path_failed", path=str(self.path), exc_info=True)
            return SubstituteAttempt(
                found=True,
                result=goal_failure(
                    f"pynixd: failed to substitute {self.path}: {exc}",
                    BuildResultStatus.MISC_FAILURE,
                ),
            )

        result = goal_success()
        log.debug("substitute_path_import_done", path=str(self.path))
        result.produced_paths.add(self.path)
        result.resolved_outputs["out"] = self.path
        return SubstituteAttempt(found=True, result=result)

    async def _import_nar(self, client: HTTPBinaryCacheClient, hit: _CacheHit) -> None:
        async with self.engine.substitution_import_limiter, self.engine.ctx.local_store.transfer_conn() as conn:
            request = AddToStoreNarRequest(
                info=hit.narinfo.valid_path_info,
                repair=0,
                dont_check_sigs=1,
            )
            await request.to_writer(WriteContext.from_conn(conn))
            await conn.w.drain()

            framed = conn.w.framed()
            async for chunk in client.stream_nar(hit):
                framed.write(chunk)
                await conn.w.drain()
            await framed.finalize()
            await request.response_type.from_reader(ReadContext.from_conn(conn))

    async def _is_valid_local_path(self, path: StorePath) -> bool:
        response = await self.engine.ctx.local_store.execute(IsValidPathRequest(path=SerdeStorePath(path=str(path))))
        return bool(response.valid)


class _PassthroughDecompressor:
    def decompress(self, chunk: bytes) -> list[bytes]:
        return [chunk]

    def finish(self) -> list[bytes]:
        return []


class _ZlibDecompressor:
    def __init__(self) -> None:
        self._decompressor = zlib.decompressobj()

    def decompress(self, chunk: bytes) -> list[bytes]:
        return [self._decompressor.decompress(chunk)]

    def finish(self) -> list[bytes]:
        return [self._decompressor.flush()]


class _GzipDecompressor:
    def __init__(self) -> None:
        self._decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def decompress(self, chunk: bytes) -> list[bytes]:
        return [self._decompressor.decompress(chunk)]

    def finish(self) -> list[bytes]:
        return [self._decompressor.flush()]


class _Bzip2Decompressor:
    def __init__(self) -> None:
        self._decompressor = bz2.BZ2Decompressor()

    def decompress(self, chunk: bytes) -> list[bytes]:
        return [self._decompressor.decompress(chunk)]

    def finish(self) -> list[bytes]:
        return []


class _XzDecompressor:
    def __init__(self) -> None:
        self._decompressor = lzma.LZMADecompressor()

    def decompress(self, chunk: bytes) -> list[bytes]:
        return [self._decompressor.decompress(chunk)]

    def finish(self) -> list[bytes]:
        return []


class _ZstdDecompressor:
    def __init__(self) -> None:
        self._decompressor = zstd.ZstdDecompressor().decompressobj()

    def decompress(self, chunk: bytes) -> list[bytes]:
        return [self._decompressor.decompress(chunk)]

    def finish(self) -> list[bytes]:
        return [self._decompressor.flush()]


def _decompressor(compression: str):
    match compression:
        case "" | "none":
            return _PassthroughDecompressor()
        case "br":
            raise RuntimeError("brotli-compressed substitutions are not supported yet")
        case "bzip2":
            return _Bzip2Decompressor()
        case "gzip":
            return _GzipDecompressor()
        case "xz":
            return _XzDecompressor()
        case "zlib":
            return _ZlibDecompressor()
        case "zstd":
            return _ZstdDecompressor()
        case _:
            raise RuntimeError(f"unsupported NAR compression: {compression}")


def _parse_narinfo(text: str, *, expected_path: StorePath) -> NarInfo | None:
    fields: dict[str, str] = {}
    sigs_raw: list[str] = []
    for line in text.splitlines():
        match = _NARINFO_KEY_RE.match(line)
        if match is None:
            continue
        key = match.group(1)
        value = match.group(2)
        if key == "Sig":
            sigs_raw.append(value)
        else:
            fields[key] = value

    path_raw = fields.get("StorePath")
    url = fields.get("URL")
    nar_hash = fields.get("NarHash")
    if not path_raw or not url or not nar_hash:
        return None

    path = StorePath(path_raw)
    if path != expected_path:
        return None

    references: set[SerdeStorePath] = set()
    for reference in fields.get("References", "").split():
        if reference:
            references.add(SerdeStorePath(path=_reference_to_store_path(path, reference)))  # pyright: ignore[reportUnhashable]
    deriver = _deriver_to_store_path(path, fields.get("Deriver", ""))
    sigs = {Signature(**Signature.from_str(raw)) for raw in sigs_raw}  # pyright: ignore[reportUnhashable]
    info = UnkeyedValidPathInfo(
        deriver=SerdeStorePath(path=deriver),
        nar_hash=NARHash(hash=nar_hash.removeprefix("sha256:")),
        references=references,  # pyright: ignore[arg-type]
        registration_time=Time(ts=int(time.time())),
        nar_size=_parse_int(fields.get("NarSize")),
        ultimate=False,
        sigs=sigs,  # pyright: ignore[arg-type]
        ca=ContentAddress(value=fields.get("CA", "")),
    )
    return NarInfo(
        path=path,
        url=url,
        compression=fields.get("Compression", "none"),
        file_hash=fields.get("FileHash", ""),
        file_size=_parse_int(fields.get("FileSize")),
        valid_path_info=ValidPathInfo(path=SerdeStorePath(path=str(path)), info=info),
    )


def _reference_to_store_path(path: StorePath, reference: str) -> str:
    if reference.startswith("/"):
        return reference
    return f"/nix/store/{reference}"


def _deriver_to_store_path(path: StorePath, deriver: str) -> str:
    if not deriver or deriver == "unknown-deriver":
        return ""
    if deriver.startswith("/"):
        return deriver
    return f"/nix/store/{deriver}"


def _parse_int(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _normalise_base_url(url: str) -> str | None:
    stripped = url.strip()
    if not stripped:
        return None
    if "://" not in stripped:
        stripped = f"https://{stripped}"
    scheme = urlsplit(stripped).scheme
    if scheme not in {"http", "https"}:
        return None
    if not stripped.endswith("/"):
        return f"{stripped}/"
    return stripped


def substituter_urls_for() -> tuple[str, ...]:
    return _DEFAULT_SUBSTITUTERS


def substituter_fingerprint(substituter_urls: tuple[str, ...]) -> str:
    payload = "\0".join(substituter_urls).encode()
    return hashlib.sha256(payload).hexdigest()


def _result_succeeded(result: GoalResult) -> bool:
    try:
        return BuildResultStatus(result.result.status).is_success
    except ValueError:
        return False
