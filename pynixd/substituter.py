"""
Nix binary cache substituter with async HTTP for parallel queries.

Implements the Nix binary cache HTTP protocol to check whether store
paths are available on remote caches.  Uses ``aiohttp`` for concurrent
HTTP requests, enabling near-parallel substituter queries during
:class:`QueryMissing`.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod

import aiohttp
import structlog

from .store_path import StorePath
from .types import SubstitutablePathInfo

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
            connector = aiohttp.TCPConnector(limit=0, force_close=True)
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
    import os

    config = os.environ.get("NIX_CONFIG", "")
    if config:
        for line in config.split("\n"):
            line = line.strip()
            if line.startswith(("substituters ", "substituters=")):
                _, _, value = line.partition("=")
                return [u.strip() for u in value.split() if u.strip()]

    return ["https://cache.nixos.org/"]
