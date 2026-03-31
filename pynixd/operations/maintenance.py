"""
Maintenance operation request/response types.

SetOptions, CollectGarbage, OptimiseStore, VerifyStore, FindRoots.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..protocol import Op

if TYPE_CHECKING:
    from ..proxy import DaemonProxy
from ..wire import NixReader, NixWriter
from .base import (
    EmptyRequest,
    EmptyResponse,
    OpRequest,
    OpResponse,
    Uint64Response,
)

log: logging.Logger = logging.getLogger(__name__)

# Silence SetOptions by default — it's extremely verbose
logging.getLogger("pynixd.operations.SetOptionsRequest").setLevel(logging.WARNING)


# ── SetOptions ───────────────────────────────────────────────────────


@dataclass
class SetOptionsRequest(OpRequest[EmptyResponse]):
    op: ClassVar[int] = Op.SetOptions
    response_type: ClassVar[type[OpResponse]] = EmptyResponse
    keep_failed: int = 0
    keep_going: int = 0
    try_fallback: int = 0
    verbosity: int = 0
    max_build_jobs: int = 0
    max_silent_time: int = 0
    _obsolete_use_build_hook: int = 0
    build_verbosity: int = 0
    _obsolete_log_type: int = 0
    _obsolete_print_build_trace: int = 0
    build_cores: int = 0
    use_substitutes: int = 0
    overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        keep_failed = await reader.read_uint64()
        keep_going = await reader.read_uint64()
        try_fallback = await reader.read_uint64()
        verbosity = await reader.read_uint64()
        max_build_jobs = await reader.read_uint64()
        max_silent_time = await reader.read_uint64()
        obsolete_use_build_hook = await reader.read_uint64()
        build_verbosity = await reader.read_uint64()
        obsolete_log_type = await reader.read_uint64()
        obsolete_print_build_trace = await reader.read_uint64()
        build_cores = await reader.read_uint64()
        use_substitutes = await reader.read_uint64()

        overrides: dict[str, str] = {}
        if version >= wire.proto(1, 12):
            n = await reader.read_uint64()
            for _ in range(n):
                k = await reader.read_string()
                v = await reader.read_string()
                overrides[k] = v

        result = cls(
            keep_failed=keep_failed,
            keep_going=keep_going,
            try_fallback=try_fallback,
            verbosity=verbosity,
            max_build_jobs=max_build_jobs,
            max_silent_time=max_silent_time,
            _obsolete_use_build_hook=obsolete_use_build_hook,
            build_verbosity=build_verbosity,
            _obsolete_log_type=obsolete_log_type,
            _obsolete_print_build_trace=obsolete_print_build_trace,
            build_cores=build_cores,
            use_substitutes=use_substitutes,
            overrides=overrides,
        )

        cls._log.debug(
            "SetOptions: keep-failed=%d keep-going=%d fallback=%d verbosity=%d "
            "max-jobs=%d max-silent-time=%d build-verbosity=%d cores=%d "
            "use-substitutes=%d",
            keep_failed,
            keep_going,
            try_fallback,
            verbosity,
            max_build_jobs,
            max_silent_time,
            build_verbosity,
            build_cores,
            use_substitutes,
        )
        return result

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> EmptyResponse:
        await cls.from_reader(proxy._r, proxy._version)
        # We don't forward SetOptions to the local store daemon as it would
        # mess with our own proxy's session state if it was a real daemon.
        # We just return EmptyResponse.
        return EmptyResponse()

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.keep_failed)
        writer.write_uint64(self.keep_going)
        writer.write_uint64(self.try_fallback)
        writer.write_uint64(self.verbosity)
        writer.write_uint64(self.max_build_jobs)
        writer.write_uint64(self.max_silent_time)
        writer.write_uint64(self._obsolete_use_build_hook)
        writer.write_uint64(self.build_verbosity)
        writer.write_uint64(self._obsolete_log_type)
        writer.write_uint64(self._obsolete_print_build_trace)
        writer.write_uint64(self.build_cores)
        writer.write_uint64(self.use_substitutes)
        if version >= wire.proto(1, 12):
            writer.write_uint64(len(self.overrides))
            for k, v in self.overrides.items():
                writer.write_string(k)
                writer.write_string(v)


# ── CollectGarbage ───────────────────────────────────────────────────


@dataclass
class CollectGarbageResponse(OpResponse):
    paths_deleted: set[str] = field(default_factory=set)
    bytes_freed: int = 0
    _obsolete: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            paths_deleted=await reader.read_string_set(),
            bytes_freed=await reader.read_uint64(),
            _obsolete=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths_deleted)
        writer.write_uint64(self.bytes_freed)
        writer.write_uint64(self._obsolete)


@dataclass
class CollectGarbageRequest(OpRequest[CollectGarbageResponse]):
    op: ClassVar[int] = Op.CollectGarbage
    response_type: ClassVar[type[OpResponse]] = CollectGarbageResponse
    action: int = 0
    paths_to_delete: set[str] = field(default_factory=set)
    ignore_liveness: int = 0
    max_freed: int = 0
    _obsolete1: int = 0
    _obsolete2: int = 0
    _obsolete3: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            action=await reader.read_uint64(),
            paths_to_delete=await reader.read_string_set(),
            ignore_liveness=await reader.read_uint64(),
            max_freed=await reader.read_uint64(),
            _obsolete1=await reader.read_uint64(),
            _obsolete2=await reader.read_uint64(),
            _obsolete3=await reader.read_uint64(),
        )

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> CollectGarbageResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.call(request, client=proxy._client)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.action)
        writer.write_string_set(self.paths_to_delete)
        writer.write_uint64(self.ignore_liveness)
        writer.write_uint64(self.max_freed)
        writer.write_uint64(self._obsolete1)
        writer.write_uint64(self._obsolete2)
        writer.write_uint64(self._obsolete3)


# ── VerifyStore ──────────────────────────────────────────────────────


@dataclass
class VerifyStoreRequest(OpRequest[Uint64Response]):
    op: ClassVar[int] = Op.VerifyStore
    response_type: ClassVar[type[OpResponse]] = Uint64Response
    check_contents: int = 0
    repair: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            check_contents=await reader.read_uint64(),
            repair=await reader.read_uint64(),
        )

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> Uint64Response:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.call(request, client=proxy._client)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.check_contents)
        writer.write_uint64(self.repair)


# ── OptimiseStore ────────────────────────────────────────────────────


@dataclass
class OptimiseStoreRequest(EmptyRequest[Uint64Response]):
    op: ClassVar[int] = Op.OptimiseStore
    response_type: ClassVar[type[OpResponse]] = Uint64Response

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> Uint64Response:
        return await proxy.local_store.call(cls(), client=proxy._client)


# ── FindRoots ────────────────────────────────────────────────────────


@dataclass
class FindRootsEntry:
    link: str = ""
    target: str = ""


@dataclass
class FindRootsResponse(OpResponse):
    roots: list[FindRootsEntry] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        roots = []
        for _ in range(n):
            link = await reader.read_string()
            target = await reader.read_string()
            roots.append(FindRootsEntry(link=link, target=target))
        return cls(roots=roots)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.roots))
        for root in self.roots:
            writer.write_string(root.link)
            writer.write_string(root.target)


@dataclass
class FindRootsRequest(EmptyRequest[FindRootsResponse]):
    op: ClassVar[int] = Op.FindRoots
    response_type: ClassVar[type[OpResponse]] = FindRootsResponse

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> FindRootsResponse:
        return await proxy.local_store.call(cls(), client=proxy._client)


# ── AddPermRoot ─────────────────────────────────────────────────────
# Nix 1.38+. Request: store path + gcRoot path. Response: gcRoot path.
# pynixd discards this — clients shouldn't create permanent roots.


@dataclass
class AddPermRootResponse(OpResponse):
    gc_root: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(gc_root=await reader.read_string())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.gc_root)


@dataclass
class AddPermRootRequest(OpRequest[AddPermRootResponse]):
    op: ClassVar[int] = Op.AddPermRoot
    response_type: ClassVar[type[OpResponse]] = AddPermRootResponse
    store_path: str = ""
    gc_root: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            store_path=await reader.read_string(),
            gc_root=await reader.read_string(),
        )

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> AddPermRootResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        # No-op: don't create permanent roots on the host.
        # Just return the requested root path as "success".
        return AddPermRootResponse(gc_root=request.gc_root)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.store_path)
        writer.write_string(self.gc_root)
