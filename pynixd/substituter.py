"""
Nix binary cache substituter with async HTTP for parallel queries.

Implements the Nix binary cache HTTP protocol to check whether store
paths are available on remote caches.  Uses ``aiohttp`` for concurrent
HTTP requests, enabling near-parallel substituter queries during
:class:`QueryMissing`.
"""

from __future__ import annotations

import asyncio
import os
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import aiohttp
import structlog

from .store_path import StorePath
from .types import SubstitutablePathInfo

if TYPE_CHECKING:
    from collections.abc import Coroutine

log = structlog.get_logger(__name__)

_NARINFO_RE = re.compile(r"^([A-Za-z]+): (.+)", re.MULTILINE)

_NARINFO_KEYS = {
    "StorePath",
    "URL",
    "Compression",
    "FileHash",
    "FileSize",
    "NarHash",
    "NarSize",
    "References",
    "Deriver",
    "Sig",
    "System",
    "CA",
}


class Substituter(ABC):
    """Abstract substituter interface.

    Substituters check whether store paths are available for
    downloading from remote caches.
    """

    @abstractmethod
    async def query_substitutable_paths(
        self,
        paths: set[StorePath],
    ) -> set[StorePath]:
        """Return the subset of *paths* that are available on this cache."""

    @abstractmethod
    async def query_substitutable_path_infos(
        self,
        paths: set[StorePath],
    ) -> dict[StorePath, SubstitutablePathInfo]:
        """Return full substitutable info for *paths* available on this cache."""


class HttpBinaryCacheSubstituter(Substituter):
    """Substituter that queries Nix binary caches over HTTP(S).

    Uses parallel HEAD requests for quick existence checks and parallel
    GET requests for full narinfo metadata.
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
        self._own_session = False
        self._semaphore = asyncio.Semaphore(concurrency)

    async def __aenter__(self) -> HttpBinaryCacheSubstituter:
        if self._session is None:
            connector = aiohttp.TCPConnector(limit=50, force_close=False)
            self._session = aiohttp.ClientSession(connector=connector)
            self._own_session = True
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def query_substitutable_paths(
        self,
        paths: set[StorePath],
    ) -> set[StorePath]:
        if not paths or self._session is None:
            return set()
        found: set[StorePath] = set()
        async with asyncio.TaskGroup() as tg:
            for p in paths:
                tg.create_task(self._check_one(p, found))
        return found

    async def query_substitutable_path_infos(
        self,
        paths: set[StorePath],
    ) -> dict[StorePath, SubstitutablePathInfo]:
        if not paths or self._session is None:
            return {}
        result: dict[StorePath, SubstitutablePathInfo] = {}
        async with asyncio.TaskGroup() as tg:
            for p in paths:
                tg.create_task(self._get_one(p, result))
        return result

    async def _check_one(self, path: StorePath, found: set[StorePath]) -> None:
        assert self._session is not None
        url = f"{self.base_url}{path.hash_part()}.narinfo"
        async with self._semaphore:
            try:
                async with self._session.head(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                    raise_for_status=False,
                ) as resp:
                    if resp.status == 200:
                        found.add(path)
            except (TimeoutError, aiohttp.ClientError, OSError):
                pass

    async def _get_one(
        self,
        path: StorePath,
        result: dict[StorePath, SubstitutablePathInfo],
    ) -> None:
        assert self._session is not None
        url = f"{self.base_url}{path.hash_part()}.narinfo"
        async with self._semaphore:
            try:
                async with self._session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                    raise_for_status=False,
                ) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()
                    info = _parse_narinfo(text)
                    if info is not None:
                        result[path] = info
            except (TimeoutError, aiohttp.ClientError, OSError):
                pass


class SubstituterGroup:
    """Race N substituters per path — first 200 wins.

    Owns the shared :class:`~aiohttp.ClientSession` and injects it
    into every substituter.  Use as an async context manager::

        async with SubstituterGroup(subs) as sg:
            sg.tg = ...  # bind a TaskGroup
            await sg.has_path(...)
            sg.spawn(...)

    Bind a :class:`~asyncio.TaskGroup` via :attr:`tg` before
    calling :meth:`spawn` or :meth:`has_path`.
    """

    def __init__(
        self,
        subs: list[HttpBinaryCacheSubstituter],
    ) -> None:
        self._subs = subs
        self.tg: asyncio.TaskGroup | None = None
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> SubstituterGroup:
        connector = aiohttp.TCPConnector(limit=50, force_close=False)
        self._session = aiohttp.ClientSession(connector=connector)
        for sub in self._subs:
            sub._session = self._session
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Schedule *coro* in the bound TaskGroup."""
        if self.tg is None:
            raise RuntimeError("SubstituterGroup.tg is not bound to a TaskGroup")
        return self.tg.create_task(coro)

    async def has_path(self, path: StorePath) -> SubstitutablePathInfo | None:
        """Race substituters — first narinfo wins, ``None`` if none have it.

        Spawns one GET task per substituter racing to fetch the narinfo
        for *path*.  Blocks until the first success or every substituter
        has responded negatively.
        """
        if self.tg is None:
            raise RuntimeError("SubstituterGroup.tg is not bound to a TaskGroup")
        if not self._subs:
            return None

        lock = asyncio.Lock()
        info_result: SubstitutablePathInfo | None = None
        remaining = len(self._subs)
        latch: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def _try(sub: HttpBinaryCacheSubstituter) -> None:
            nonlocal info_result, remaining
            try:
                infos = await sub.query_substitutable_path_infos({path})
            except asyncio.CancelledError:
                raise
            except Exception:
                infos = {}
            async with lock:
                if infos and info_result is None:
                    info_result = infos[path]
                    if not latch.done():
                        latch.set_result(None)
                remaining -= 1
                if remaining == 0 and not latch.done():
                    latch.set_result(None)

        for sub in self._subs:
            self.tg.create_task(_try(sub))

        await latch
        return info_result


def _parse_narinfo(text: str) -> SubstitutablePathInfo | None:
    fields: dict[str, str] = {}
    for match in _NARINFO_RE.finditer(text):
        key = match.group(1)
        value = match.group(2)
        if key in _NARINFO_KEYS:
            fields[key] = value

    if "StorePath" not in fields:
        return None

    file_size_raw = fields.get("FileSize", "0")
    nar_size_raw = fields.get("NarSize", "0")
    refs_raw = fields.get("References", "")
    deriver_raw = fields.get("Deriver", "")

    try:
        download_size = int(file_size_raw) if file_size_raw else 0
    except ValueError:
        download_size = 0
    try:
        nar_size = int(nar_size_raw) if nar_size_raw else 0
    except ValueError:
        nar_size = 0

    references: set[StorePath] = {StorePath(r) for r in refs_raw.split() if r}
    deriver = StorePath(deriver_raw) if deriver_raw else StorePath("")

    return SubstitutablePathInfo(
        deriver=deriver,
        references=references,
        download_size=download_size,
        nar_size=nar_size,
    )


def get_substituter_urls() -> list[str]:
    """Return configured substituter URLs.

    Reads from the ``NIX_CONFIG`` environment variable (which may
    contain ``substituters = ...``).  Falls back to a default cache.
    """
    config = os.environ.get("NIX_CONFIG", "")
    if config:
        for line in config.split("\n"):
            line = line.strip()
            if line.startswith(("substituters ", "substituters=")):
                _, _, value = line.partition("=")
                return [u.strip() for u in value.split() if u.strip()]

    return ["https://cache.nixos.org/"]
