"""Substitution — query and download store paths from Nix binary caches.

Provides a substituter abstraction with parallel query racing across
multiple upstream caches and streaming NAR download directly into a
destination store via ``AddToStoreNar``.

Architecture::

    SubstitutionManager
    ├── check(path)             → SubstitutablePathInfo | None
    ├── check(paths)            → {StorePath: SubstitutablePathInfo}
    ├── substitute(path, store) → bool
    └── substitute(paths, store)→ {StorePath: bool}
"""

from __future__ import annotations

import contextlib
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import aiohttp
import anyio
import structlog

from .nar_info import NarInfo
from .store_path import DrvOutput, StorePath
from .types.ca import Realisation

if TYPE_CHECKING:
    from .store import DaemonStore
    from .types.path_info import SubstitutablePathInfo
    from .wire import NixWriter

log = structlog.get_logger(__name__)

_NARINFO_KEY_RE = re.compile(r"^([A-Za-z]+):\s*(.*)", re.MULTILINE)


# ═════════════════════════════════════════════════════════════════════════════
# Substituter ABC
# ═════════════════════════════════════════════════════════════════════════════


class Substituter(ABC):
    """Abstract substituter — query and download from a remote cache.

    Concrete implementations handle different transport mechanisms
    (HTTP, S3, Nix daemon protocol, local files).
    """

    @abstractmethod
    async def query_paths(self, paths: set[StorePath]) -> set[StorePath]:
        """Return the subset of *paths* available on this cache (fast check)."""

    @abstractmethod
    async def query_path_infos(self, paths: set[StorePath]) -> dict[StorePath, NarInfo]:
        """Return full :class:`NarInfo` for *paths* available on this cache."""

    @abstractmethod
    async def substitute(
        self,
        paths: set[StorePath],
        infos: dict[StorePath, NarInfo],
        store: DaemonStore,
    ) -> dict[StorePath, bool]:
        """Download NARs for *paths* and import them into *store*.

        Args:
            paths: Store paths to download.
            infos: Pre-queried :class:`NarInfo` for each path (must include
                the correct ``url`` relative to this substituter's base).
            store: Destination store (e.g., a :class:`LocalStore`).

        Returns:
            ``{path: True}`` for successfully imported paths,
            ``{path: False}`` for failures.
        """

    @abstractmethod
    async def query_realisations(
        self,
        drv_outputs: set[DrvOutput],
    ) -> dict[DrvOutput, Realisation]:
        """Query content-addressed realisations by ``DrvOutput`` key.

        For each ``DrvOutput`` (e.g. ``sha256:abc...!out``) that this
        cache knows about, return the corresponding :class:`Realisation`
        with the actual ``outPath``.
        """


# ═════════════════════════════════════════════════════════════════════════════
# HTTP binary cache substituter
# ═════════════════════════════════════════════════════════════════════════════


class HttpBinaryCacheSubstituter(Substituter):
    """Substituter that queries Nix binary caches over HTTP(S).

    Uses the standard Nix binary cache protocol:
    - ``HEAD {base}/<hash>.narinfo`` for existence checks
    - ``GET {base}/<hash>.narinfo`` for full metadata
    - ``GET {base}/<nar_url>`` for NAR download with on-the-fly decompression

    The ``session`` is injected by :class:`SubstitutionManager` so that
    all substituters share a single connection pool.
    """

    def __init__(
        self,
        base_url: str,
        session: aiohttp.ClientSession | None = None,
        concurrency: int = 50,
    ) -> None:
        if not base_url.endswith("/"):
            base_url += "/"
        self.base_url = base_url
        self._session = session
        self._semaphore = anyio.Semaphore(concurrency)
        self._query_timeout = aiohttp.ClientTimeout(total=10)
        self._download_timeout = aiohttp.ClientTimeout(total=300)

    # ── Query interface ─────────────────────────────────────────────

    async def query_paths(self, paths: set[StorePath]) -> set[StorePath]:
        """Fast existence check via parallel HEAD requests."""
        if not paths or self._session is None:
            return set()
        found: set[StorePath] = set()
        async with anyio.create_task_group() as tg:
            for p in paths:
                tg.start_soon(self._check_one, p, found)
        return found

    async def _check_one(self, path: StorePath, found: set[StorePath]) -> None:
        url = f"{self.base_url}{path.hash_part()}.narinfo"
        async with self._semaphore:
            try:
                assert self._session is not None
                async with self._session.head(
                    url,
                    timeout=self._query_timeout,
                    raise_for_status=False,
                ) as resp:
                    if resp.status == 200:
                        found.add(path)
            except (TimeoutError, aiohttp.ClientError, OSError):
                pass

    async def query_path_infos(
        self,
        paths: set[StorePath],
    ) -> dict[StorePath, NarInfo]:
        """Fetch full narinfo metadata via parallel GET requests."""
        if not paths or self._session is None:
            return {}
        result: dict[StorePath, NarInfo] = {}
        async with anyio.create_task_group() as tg:
            for p in paths:
                tg.start_soon(self._get_narinfo, p, result)
        return result

    async def _get_narinfo(
        self,
        path: StorePath,
        result: dict[StorePath, NarInfo],
    ) -> None:
        url = f"{self.base_url}{path.hash_part()}.narinfo"
        async with self._semaphore:
            try:
                assert self._session is not None
                async with self._session.get(
                    url,
                    timeout=self._query_timeout,
                    raise_for_status=False,
                ) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()
                    info = _parse_narinfo(text, path)
                    if info is not None:
                        result[path] = info
            except (TimeoutError, aiohttp.ClientError, OSError):
                pass

    # ── Download interface ──────────────────────────────────────────

    async def query_realisations(
        self,
        drv_outputs: set[DrvOutput],
    ) -> dict[DrvOutput, Realisation]:
        """Query CA realisations by DrvOutput from the binary cache.

        Realisations are stored at ``{base}/realisation/{drv_output}.json``.
        """
        if not drv_outputs or self._session is None:
            return {}
        result: dict[DrvOutput, Realisation] = {}
        async with anyio.create_task_group() as tg:
            for d in drv_outputs:
                tg.start_soon(self._get_realisation, d, result)
        return result

    async def _get_realisation(
        self,
        drv_output: DrvOutput,
        result: dict[DrvOutput, Realisation],
    ) -> None:
        url = f"{self.base_url}realisation/{drv_output}.json"
        async with self._semaphore:
            try:
                assert self._session is not None
                async with self._session.get(
                    url,
                    timeout=self._query_timeout,
                    raise_for_status=False,
                ) as resp:
                    if resp.status != 200:
                        return
                    result[drv_output] = Realisation.model_validate(await resp.json())
            except (TimeoutError, aiohttp.ClientError, OSError):
                pass

    async def substitute(
        self,
        paths: set[StorePath],
        infos: dict[StorePath, NarInfo],
        store: DaemonStore,
    ) -> dict[StorePath, bool]:
        """Download and import NARs in parallel.

        Each path is downloaded independently via ``AddToStoreNar``
        with on-the-fly decompression.
        """
        if not paths or self._session is None:
            return {}

        results: dict[StorePath, bool] = {}
        async with anyio.create_task_group() as tg:
            for path in paths:
                info = infos.get(path)
                if info is None:
                    results[path] = False
                    continue
                tg.start_soon(self._substitute_one, path, info, store, results)
        return results

    async def _substitute_one(
        self,
        path: StorePath,
        info: NarInfo,
        store: DaemonStore,
        results: dict[StorePath, bool],
    ) -> None:
        assert self._session is not None
        url = f"{self.base_url}{info.url}"

        try:
            async with self._session.get(
                url,
                timeout=self._download_timeout,
                raise_for_status=True,
            ) as resp:
                path_info = info.to_valid_path_info()
                decompressor = _make_decompressor(info.compression)

                async def _provider(writer: NixWriter) -> None:
                    async for chunk, _ in resp.content.iter_chunks():
                        data = decompressor.decompress(chunk)
                        if data:
                            writer.write_uint64(len(data))
                            writer.write(data)
                    # Flush remaining decompressed data.
                    # zstandard uses flush(); lzma/bz2 use decompress(b"").
                    remaining = b""
                    flush = getattr(decompressor, "flush", None)
                    if flush is not None:
                        remaining = flush()
                    else:
                        with contextlib.suppress(EOFError):
                            remaining = decompressor.decompress(b"")
                    if remaining:
                        writer.write_uint64(len(remaining))
                        writer.write(remaining)
                    writer.write_uint64(0)
                    await writer.drain()

                from .operations.add_to_store_nar import AddToStoreNarRequest

                await AddToStoreNarRequest(
                    info=path_info,
                    repair=0,
                    dont_check_sigs=1,
                    async_provider=_provider,
                ).execute(store)

                store.tracker.add_known_path(path)
                results[path] = True

        except Exception:
            log.exception("substitute_path_failed", path=str(path), url=url)
            results[path] = False


# ═════════════════════════════════════════════════════════════════════════════
# Daemon-backed substituter
# ═════════════════════════════════════════════════════════════════════════════


class StoreSubstituter(Substituter):
    """Substituter backed by a Nix daemon :class:`~.store.Store`.

    Delegates query and substitution to a connected Nix daemon via the
    daemon protocol.  The daemon uses its own configured substituters
    (set via ``NIX_CONFIG``) to download and import paths.

    ``substitute()`` streams NARs from the wrapped daemon via
    ``NarFromPath → AddToStoreNar`` directly into the destination store.
    """

    def __init__(self, store: DaemonStore) -> None:
        self._store = store

    async def query_paths(self, paths: set[StorePath]) -> set[StorePath]:
        """Check existence via ``QueryValidPaths(substitute=0)``."""
        if not paths:
            return set()
        from .operations.query_valid_paths import QueryValidPathsRequest

        resp = await self._store.execute(
            QueryValidPathsRequest(paths=paths, substitute=0),
        )
        return resp.paths

    async def query_path_infos(
        self,
        paths: set[StorePath],
    ) -> dict[StorePath, NarInfo]:
        """Fetch path metadata via ``QueryPathInfo`` (parallel)."""
        if not paths:
            return {}

        result: dict[StorePath, NarInfo] = {}
        async with anyio.create_task_group() as tg:
            for path in paths:
                tg.start_soon(self._query_one_info, path, result)
        return result

    async def _query_one_info(
        self,
        path: StorePath,
        result: dict[StorePath, NarInfo],
    ) -> None:
        from .operations.query_path_info import QueryPathInfoRequest

        try:
            resp = await self._store.execute(QueryPathInfoRequest(path=path))
        except Exception:
            return
        info = resp.info
        if info is None:
            return
        result[path] = NarInfo(
            store_path=path,
            url="",
            compression="none",
            nar_hash=info.nar_hash,
            nar_size=info.nar_size,
            references=info.references,
            deriver=info.deriver,
            ca=info.ca,
            sigs=info.sigs,
        )

    async def substitute(
        self,
        paths: set[StorePath],
        infos: dict[StorePath, NarInfo],
        store: DaemonStore,
    ) -> dict[StorePath, bool]:
        """Stream NARs from the wrapped daemon into *store* via ``NarFromPath → AddToStoreNar``.

        Uses the nar_size from query results to read the exact number
        of unframed NAR bytes from the source, then pipes them as
        framed chunks into the destination.
        """
        if not paths or not infos:
            return {}

        results: dict[StorePath, bool] = {}
        async with anyio.create_task_group() as tg:
            for path in paths:
                info = infos.get(path)
                if info is None:
                    results[path] = False
                    continue
                tg.start_soon(
                    self._substitute_one,
                    path,
                    info,
                    store,
                    results,
                )
        return results

    # ── CA realisation queries ───────────────────────────────────

    async def query_realisations(
        self,
        drv_outputs: set[DrvOutput],
    ) -> dict[DrvOutput, Realisation]:
        """Query CA realisations via the daemon's ``QueryRealisation`` operation."""
        if not drv_outputs:
            return {}
        result: dict[DrvOutput, Realisation] = {}
        async with anyio.create_task_group() as tg:
            for d in drv_outputs:
                tg.start_soon(self._query_one_realisation, d, result)
        return result

    async def _query_one_realisation(
        self,
        drv_output: DrvOutput,
        result: dict[DrvOutput, Realisation],
    ) -> None:
        from .operations.ca_derivations import QueryRealisationRequest

        try:
            resp = await self._store.execute(QueryRealisationRequest(drv_output=drv_output))
            if resp.realisations:
                result[drv_output] = resp.realisations[0]
        except Exception:
            pass

    async def _substitute_one(
        self,
        path: StorePath,
        info: NarInfo,
        dst_store: DaemonStore,
        results: dict[StorePath, bool],
    ) -> None:
        from .operations.add_to_store_nar import AddToStoreNarRequest, AddToStoreNarResponse
        from .operations.nar_from_path import NarFromPathRequest
        from .types.context import ReadContext, WriteContext
        from .wire import _CHUNK_SIZE

        try:
            path_info = info.to_valid_path_info()

            # Open destination connection (AddToStoreNar)
            async with dst_store.transfer_conn() as dst:
                add_req = AddToStoreNarRequest(
                    info=path_info,
                    repair=0,
                    dont_check_sigs=1,
                )
                await add_req.serialize(WriteContext.from_conn(dst))

                # Open source connection (NarFromPath)
                async with self._store.transfer_conn() as src:
                    nar_req = NarFromPathRequest(
                        path=path,
                        nar_size=info.nar_size,
                    )
                    await nar_req.serialize(WriteContext.from_conn(src))
                    await src.w.drain()

                    # Drain stderr from NarFromPath response,
                    # then pipe nar_size unframed NAR bytes as framed chunks
                    await src.r.drain_stderr()
                    remaining = info.nar_size
                    while remaining > 0:
                        to_read = min(remaining, _CHUNK_SIZE)
                        chunk = await src.r.readexactly(to_read)
                        dst.w.write_uint64(len(chunk))
                        dst.w.write(chunk)
                        remaining -= to_read

                    dst.w.write_uint64(0)
                    await dst.w.drain()

                # Read AddToStoreNar response from destination
                await AddToStoreNarResponse.deserialize(
                    ReadContext(reader=dst.r, version=dst_store.version),
                )

                dst_store.tracker.add_known_path(path)
                results[path] = True

        except Exception:
            log.exception("store_substitute_failed", path=str(path))
            results[path] = False


class SubstitutionManager:
    """Orchestrates substituters with parallel query racing and DAG ordering.

    Creates a shared ``aiohttp.ClientSession`` on construction — no need for
    ``async with`` unless you want deterministic cleanup::

        sm = SubstitutionManager([HttpBinaryCacheSubstituter("https://cache.nixos.org")])
        info = await sm.query_path(path)
        ok = await sm.substitute_path(path, store)
        await sm.close()  # optional explicit cleanup

    Or with async context manager for auto-cleanup::

        async with SubstitutionManager(...) as sm:
            info = await sm.query_path(path)
    """

    def __init__(
        self,
        substituters: list[Substituter],
        retries_per_sub: int = 3,
    ) -> None:
        self._subs = substituters
        self._retries = retries_per_sub
        connector = aiohttp.TCPConnector(limit=50, force_close=False)
        self._session = aiohttp.ClientSession(connector=connector)
        for sub in self._subs:
            if isinstance(sub, HttpBinaryCacheSubstituter):
                sub._session = self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> SubstitutionManager:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── query_path / query_paths ──────────────────────────────────

    async def query_path(self, path: StorePath) -> SubstitutablePathInfo | None:
        """Check if a single store path is available from any substituter.

        Returns :class:`SubstitutablePathInfo` or ``None`` if not found.
        """
        result = await self.query_paths({path})
        return result.get(path)

    async def query_paths(
        self,
        paths: set[StorePath],
    ) -> dict[StorePath, SubstitutablePathInfo]:
        """Race all substituters — first ``SubstitutablePathInfo`` per path wins.

        Returns the lean :class:`SubstitutablePathInfo` used by the daemon
        protocol (deriver, references, download size, NAR size) instead of
        the full ``NarInfo`` (URL, compression, signatures, ...).
        """
        if not paths or not self._subs:
            return {}

        from .types.path_info import SubstitutablePathInfo

        result: dict[StorePath, SubstitutablePathInfo] = {}
        lock = anyio.Lock()
        remaining = set(paths)

        async def _race(sub: Substituter) -> None:
            nonlocal remaining
            if not remaining:
                return
            infos = await sub.query_path_infos(remaining.copy())
            async with lock:
                for p, info in infos.items():
                    if p in remaining and p not in result:
                        result[p] = SubstitutablePathInfo(
                            deriver=info.deriver,
                            references=info.references,
                            download_size=info.file_size or info.nar_size,
                            nar_size=info.nar_size,
                        )
                        remaining.discard(p)

        async with anyio.create_task_group() as tg:
            for sub in self._subs:
                tg.start_soon(_race, sub)

        return result

    # ── substitute_path / substitute_paths ─────────────────────────

    async def substitute_path(self, path: StorePath, store: DaemonStore) -> bool:
        """Download and import a single store path."""
        result = await self.substitute_paths({path}, store)
        return result.get(path, False)

    async def substitute_paths(
        self,
        paths: set[StorePath],
        store: DaemonStore,
    ) -> dict[StorePath, bool]:
        """Download and import paths via available substituters.

        Distributes paths across all registered substituters and runs
        them in parallel.  Each substituter handles its own download
        and ``AddToStoreNar`` import.

        Returns ``{path: True/False}`` for the requested paths.
        """
        if not paths:
            return {}

        results: dict[StorePath, bool] = {}

        async def _sub_for_sub(
            sub: Substituter,
            sub_paths: set[StorePath],
        ) -> None:
            if not sub_paths:
                return
            infos = await sub.query_path_infos(sub_paths)
            sub_results = await sub.substitute(sub_paths, infos, store)
            results.update(sub_results)

        async with anyio.create_task_group() as tg:
            for sub in self._subs:
                tg.start_soon(_sub_for_sub, sub, paths.copy())

        for p in paths:
            results.setdefault(p, False)

        return results

    # ── query_realisations ──────────────────────────────────────────

    async def query_realisations(
        self,
        drv_outputs: set[DrvOutput],
    ) -> dict[DrvOutput, Realisation]:
        """Query CA realisations from all substituters — first hit per DrvOutput wins."""
        if not drv_outputs or not self._subs:
            return {}

        result: dict[DrvOutput, Realisation] = {}
        lock = anyio.Lock()
        remaining = set(drv_outputs)

        async def _race(sub: Substituter) -> None:
            nonlocal remaining
            if not remaining:
                return
            infos = await sub.query_realisations(remaining.copy())
            async with lock:
                for d, r in infos.items():
                    if d in remaining and d not in result:
                        result[d] = r
                        remaining.discard(d)

        async with anyio.create_task_group() as tg:
            for sub in self._subs:
                tg.start_soon(_race, sub)

        return result


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════


def _parse_narinfo(text: str, path: StorePath) -> NarInfo | None:
    """Parse a ``.narinfo`` file into a :class:`NarInfo`.

    Handles multiple ``Sig:`` lines by accumulating.
    """
    fields: dict[str, str] = {}
    sigs_list: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NARINFO_KEY_RE.match(line)
        if m is None:
            continue
        key = m.group(1)
        value = m.group(2)

        if key == "Sig":
            sigs_list.append(value)
        else:
            fields[key] = value

    if "StorePath" not in fields:
        return None

    def _str(k: str) -> str:
        return fields.get(k, "")

    def _int(k: str) -> int:
        try:
            return int(_str(k))
        except ValueError:
            return 0

    references: set[StorePath] = set()
    for r in _str("References").split():
        if r:
            if not r.startswith("/nix/store/"):
                r = f"/nix/store/{r}"
            references.add(StorePath(r))

    deriver_raw = _str("Deriver")
    if deriver_raw:
        if not deriver_raw.startswith("/nix/store/"):
            deriver_raw = f"/nix/store/{deriver_raw}"
        deriver = StorePath(deriver_raw)
    else:
        deriver = StorePath("")

    sigs: set[str] = set(sigs_list)

    return NarInfo(
        store_path=path,
        url=_str("URL"),
        compression=_str("Compression") or "none",
        nar_hash=_str("NarHash"),
        nar_size=_int("NarSize"),
        file_hash=_str("FileHash"),
        file_size=_int("FileSize"),
        references=references,
        deriver=deriver,
        system=_str("System"),
        ca=_str("CA"),
        sigs=sigs,
    )


def _make_decompressor(compression: str):
    """Return an incremental decompressor for *compression*.

    Returns an object with ``decompress(data) -> bytes`` and
    (optionally) ``flush() -> bytes``.  Raises :class:`ValueError` for
    unknown formats.
    """
    if compression in ("", "none"):
        return _PassthroughDecompressor()
    if compression == "xz":
        import lzma

        return lzma.LZMADecompressor()
    if compression == "bzip2":
        import bz2

        return bz2.BZ2Decompressor()
    if compression == "zstd":
        import zstandard as zstd

        return zstd.ZstdDecompressor().decompressobj()
    raise ValueError(f"Unsupported compression format: {compression!r}")


class _PassthroughDecompressor:
    """No-op decompressor for uncompressed NARs."""

    @staticmethod
    def decompress(data: bytes) -> bytes:
        return data

    @staticmethod
    def flush() -> bytes:
        return b""
