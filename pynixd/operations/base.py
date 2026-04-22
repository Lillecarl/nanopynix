"""
Base classes and common types for nix daemon operation serialization.

Each operation has a request and response dataclass with factory methods:
- from_reader(reader) — reads wire data into a Python object
- to_writer(writer)   — writes the Python object back to wire

These are the canonical way to speak the daemon protocol everywhere:
proxy, store, backend, scheduler, etc.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar

import structlog

from .. import wire, derived_path as derived_path_mod
from ..exceptions import OpNotImplementedError, BackendError
from ..stderr import StderrError, read_stream
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from ..types import (
    BasicDerivation as BasicDerivation,
    BuildMode as BuildMode,
    BuildResult as BuildResult,
    BuildResultStatus as BuildResultStatus,
    DerivationOutput as DerivationOutput,
    OutputKind as OutputKind,
    Role as Role,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..stderr import StderrError, StderrMsg
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RequestContext:
    """Context passed to operation handlers."""

    proxy: DaemonProxy
    role: Role
    version: int
    username: str


# ── Base classes ─────────────────────────────────────────────────────

Resp = TypeVar("Resp", bound="OpResponse")

# Auto-populated by OpRequest.__init_subclass__
OP_REGISTRY: dict[int, type[OpRequest[Any]]] = {}


@dataclass
class OpRequest(ABC, Generic[Resp]):
    """Base class for operation requests.

    Subclasses that set ``op`` in their own class body are automatically
    registered in :data:`OP_REGISTRY` for wire-protocol dispatch.
    """

    op: ClassVar[int]
    name: ClassVar[str]
    response_type: ClassVar[type[OpResponse]]
    is_query: ClassVar[bool] = False
    is_build: ClassVar[bool] = False
    is_extension: ClassVar[bool] = False
    # logger is set per-subclass by __init_subclass__; instances override it
    # via self.logger = self.logger.bind(identifier=...) in from_reader/to_writer.
    # Declared without type annotation so dataclass ignores it.
    logger = structlog.get_logger("pynixd.operations.base")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Runs when an OpRequest subclass is instantiated, registers
        the subclass in OP_REGISTRY"""
        super().__init_subclass__(**kwargs)
        if "op" in cls.__dict__:
            OP_REGISTRY[cls.op] = cls
        cls.logger = structlog.get_logger(f"pynixd.operations.{cls.__name__}")

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        """Handle this operation from a client.

        Decodes the request and delegates execution to the stores.
        Streaming operations should override this method.
        """
        await self.from_reader(ctx.proxy.r, ctx.version)
        result = await ctx.proxy.execute(self)
        return result

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> Resp:
        """Execute this operation on a store and return a buffered response.

        Falls back to the wire protocol (standard ops) or extension negotiation
        for extension operations.
        """
        if self.is_extension:
            await store.probe()

            feature_name = type(self).name
            if feature_name in store.features:
                from ..testing import set_test_value

                set_test_value(f"{feature_name}_delegated", True)
                return await store.call(
                    self, client=client, suppress_last=suppress_last
                )

            raise OpNotImplementedError(
                f"Extension operation {type(self).__name__} (op={self.op}) "
                "not supported by this store (no DB and no wire fallback)"
            )

        return await store.call(
            self,
            client=client,
            suppress_last=suppress_last,
        )

    @abstractmethod
    async def from_reader(self, reader: NixReader, version: int) -> Self: ...

    @abstractmethod
    async def to_writer(self, writer: NixWriter, version: int) -> None: ...


@dataclass
class OperationLogs:
    """Container for stderr messages from an operation.

    Collects all stderr messages (NEXT, START_ACTIVITY, STOP_ACTIVITY,
    RESULT, ERROR) but NOT LAST — that's injected by to_writer.
    """

    messages: list[StderrMsg] = field(default_factory=list)

    @property
    def error(self) -> StderrError | None:
        """First StderrError in messages, if any."""
        for msg in self.messages:
            if isinstance(msg, StderrError):
                return msg
        return None

    @property
    def has_error(self) -> bool:
        """True if any StderrError in messages."""
        return self.error is not None

    def __bool__(self) -> bool:
        """Falsy if has errors."""
        return not self.has_error

    def add(self, msg: StderrMsg) -> None:
        """Add a stderr message to the collection."""
        self.messages.append(msg)

    def to_writer(self, writer: NixWriter) -> None:
        """Write all messages followed by STDERR_LAST."""
        for msg in self.messages:
            msg.to_writer(writer)
        writer.write_uint64(wire.STDERR_LAST)

    async def from_reader(
        self,
        reader: NixReader,
        client: ClientConn | None = None,
        buffer: bool = True,
    ) -> Self:
        """Read stderr messages until STDERR_LAST from reader.

        If client is provided, messages are queued for real-time delivery.
        If buffer is False, messages are NOT added to self.messages.

        Raises BackendError if a StderrError is received from the daemon,
        since the daemon sends no response payload after an error.
        """

        async for msg in read_stream(reader):
            if client:
                client.queue.put_nowait(msg)
            if buffer:
                self.add(msg)
            if isinstance(msg, StderrError):
                raise BackendError(f"Daemon error ({msg.error_type}): {msg.msg}")
        return self


@dataclass
class OpResponse(ABC):
    """Base class for operation responses."""

    logs: OperationLogs = field(default_factory=OperationLogs)
    # logger is set per-subclass by __init_subclass__; instances override it
    # via self.logger = self.logger.bind(identifier=...) in from_reader/to_writer.
    # Declared without type annotation so dataclass ignores it.
    logger = structlog.get_logger("pynixd.operations.base")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.logger = structlog.get_logger(f"pynixd.operations.{cls.__name__}")

    @property
    def is_not_found(self) -> bool:
        """True if this response indicates that the requested data was not found.
        Used by proxy to decide whether to try other stores.
        """
        return False

    @abstractmethod
    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self: ...

    @abstractmethod
    async def to_writer(self, writer: NixWriter, version: int) -> None: ...


# ── Complex structures ───────────────────────────────────────────────


@dataclass
class UnkeyedValidPathInfo:
    """Metadata for a store path (without the path itself)."""

    deriver: StorePath = field(default_factory=lambda: StorePath(""))
    nar_hash: str = ""
    references: set[StorePath] = field(default_factory=set)
    registration_time: int = 0
    nar_size: int = 0
    ultimate: int = 0
    sigs: set[str] = field(default_factory=set)
    ca: str = ""

    async def from_reader(self, reader: NixReader) -> Self:
        """Read UnkeyedValidPathInfo from wire."""
        self.deriver = await reader.read_string(StorePath)
        self.nar_hash = await reader.read_string()
        self.references = await reader.read_string_set(StorePath)
        self.registration_time = await reader.read_uint64()
        self.nar_size = await reader.read_uint64()
        self.ultimate = await reader.read_uint64()
        self.sigs = await reader.read_string_set()
        self.ca = await reader.read_string()
        return self

    def to_writer(self, writer: NixWriter) -> None:
        """Write UnkeyedValidPathInfo to wire."""
        writer.write_string(self.deriver)
        # Strip sha256: prefix if present (Nix expects hex or nix32 without prefix)
        nar_hash = self.nar_hash
        if nar_hash.startswith("sha256:"):
            nar_hash = nar_hash[7:]
        writer.write_string(nar_hash)
        writer.write_string_set(self.references)
        writer.write_uint64(self.registration_time)
        writer.write_uint64(self.nar_size)
        writer.write_uint64(self.ultimate)
        writer.write_string_set(self.sigs)
        writer.write_string(self.ca)

    def with_path(self, path: StorePath) -> ValidPathInfo:
        """Create a ValidPathInfo by adding a path to this metadata."""
        return ValidPathInfo(
            path=path,
            deriver=self.deriver,
            nar_hash=self.nar_hash,
            references=self.references,
            registration_time=self.registration_time,
            nar_size=self.nar_size,
            ultimate=self.ultimate,
            sigs=self.sigs,
            ca=self.ca,
        )


@dataclass
class ValidPathInfo(UnkeyedValidPathInfo):
    """Metadata for a store path (including the path)."""

    path: StorePath = field(default_factory=lambda: StorePath(""))

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ValidPathInfo):
            return False
        return self.path == other.path

    async def from_reader(self, reader: NixReader) -> Self:
        """Read ValidPathInfo (path + UnkeyedValidPathInfo) from wire."""
        path = await reader.read_string(StorePath)
        info = await UnkeyedValidPathInfo().from_reader(reader)
        return info.with_path(path)  # type: ignore[return-value]

    def to_writer(self, writer: NixWriter) -> None:
        """Write ValidPathInfo (path + UnkeyedValidPathInfo) to wire."""
        writer.write_string(self.path)
        super().to_writer(writer)

    def to_bytes(self) -> bytes:
        """Serialize ValidPathInfo to wire format as bytes."""
        buf = wire.BytesWriter()
        self.to_writer(buf)
        return buf.get_bytes()

    @classmethod
    def from_narinfo(cls, content: str) -> ValidPathInfo:
        """Parse ValidPathInfo from .narinfo file content."""
        data: dict[str, Any] = {
            "references": set(),
            "sigs": set(),
        }
        for line in content.splitlines():
            line = line.strip()
            if not line or ":" not in line or line.startswith("#"):
                continue
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()

            if key == "StorePath":
                data["path"] = StorePath(val)
            elif key == "NarHash":
                data["nar_hash"] = val
            elif key == "NarSize":
                data["nar_size"] = int(val)
            elif key == "References":
                for r in val.split():
                    if r:
                        if r.startswith("/nix/store/"):
                            data["references"].add(StorePath(r))
                        else:
                            data["references"].add(StorePath(f"/nix/store/{r}"))
            elif key == "Deriver":
                if val:
                    if val.startswith("/nix/store/"):
                        data["deriver"] = StorePath(val)
                    else:
                        data["deriver"] = StorePath(f"/nix/store/{val}")
                else:
                    data["deriver"] = StorePath("")
            elif key == "Sig":
                data["sigs"].add(val)
            elif key == "CA":
                data["ca"] = val

        return cls(
            path=data.get("path", StorePath("")),
            deriver=data.get("deriver", StorePath("")),
            nar_hash=data.get("nar_hash", ""),
            references=data.get("references", set()),
            nar_size=data.get("nar_size", 0),
            sigs=data.get("sigs", set()),
            ca=data.get("ca", ""),
        )

    def to_narinfo(self) -> str:
        """Format ValidPathInfo as .narinfo file content."""
        # Ensure NarHash has sha256: prefix (expected by Nix)
        nar_hash = self.nar_hash
        if not nar_hash.startswith("sha256:"):
            nar_hash = f"sha256:{nar_hash}"

        nar_hash_part = nar_hash.split(":")[-1]

        lines = [
            f"StorePath: {self.path}",
            f"URL: nar/{nar_hash_part}.nar",
            "Compression: none",
            f"NarHash: {nar_hash}",
            f"NarSize: {self.nar_size}",
        ]

        if self.references:
            # references are stored as full StorePath, Nix expects base name
            def strip_prefix(p: str) -> str:
                return p.split("/")[-1]

            refs = " ".join(sorted(strip_prefix(str(r)) for r in self.references))
            lines.append(f"References: {refs}")

        if self.deriver:
            lines.append(f"Deriver: {self.deriver.split('/')[-1]}")

        for sig in sorted(self.sigs):
            lines.append(f"Sig: {sig}")

        if self.ca:
            lines.append(f"CA: {self.ca}")

        return "\n".join(lines) + "\n"


@dataclass
class SubstitutablePathInfo:
    """Metadata for a substitutable path (missing but available)."""

    deriver: StorePath = field(default_factory=lambda: StorePath(""))
    references: set[StorePath] = field(default_factory=set)
    download_size: int = 0
    nar_size: int = 0

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.deriver = await reader.read_string(StorePath)
        self.references = await reader.read_string_set(StorePath)
        self.download_size = await reader.read_uint64()
        self.nar_size = await reader.read_uint64()
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.deriver)
        writer.write_string_set(self.references)
        writer.write_uint64(self.download_size)
        writer.write_uint64(self.nar_size)


@dataclass
class BuiltOutput:
    """A built output - either plain path or content-addressed (CA) format.

    Modern Nix uses CA format where the value is a JSON object like:
        {"outPath": "/nix/store/xxx", "hash": "...", "mode": "..."}

    We parse this into a proper class so we can work with it easily.
    """

    out_path: StorePath = field(default_factory=lambda: StorePath(""))
    ca: str = ""  # content-addressed hash info (CA, text hash, or fixed)
    hash: str = ""  # hash digest
    hash_algo: str = ""  # hash algorithm (sha256, etc.)
    nar_hash: str = ""  # NAR hash
    nar_size: int = 0  # NAR size
    reference: str = ""  # reference (for CA)

    @classmethod
    def from_string(cls, s: str) -> BuiltOutput:
        """Parse from string - could be plain path or JSON CA info."""
        if not s:
            return cls()

        # Try to parse as JSON - if it fails, treat as plain path
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return cls(
                    out_path=StorePath(data.get("outPath", "")),
                    ca=data.get("ca", ""),
                    hash=data.get("hash", ""),
                    hash_algo=data.get("hashAlgo", ""),
                    nar_hash=data.get("narHash", ""),
                    nar_size=data.get("narSize", 0),
                    reference=data.get("reference", ""),
                )
        except (json.JSONDecodeError, TypeError):
            pass

        # Plain path format
        return cls(out_path=StorePath(s))

    def to_string(self) -> str:
        """Serialize back to the format the daemon expects."""
        # If we have CA info, serialize as JSON
        if self.ca or self.hash or self.nar_hash:
            data: dict[str, str | int] = {"outPath": self.out_path}
            if self.ca:
                data["ca"] = self.ca
            if self.hash:
                data["hash"] = self.hash
            if self.hash_algo:
                data["hashAlgo"] = self.hash_algo
            if self.nar_hash:
                data["narHash"] = self.nar_hash
            if self.nar_size:
                data["narSize"] = self.nar_size
            if self.reference:
                data["reference"] = self.reference
            return json.dumps(data)
        # Plain path
        return self.out_path


# Silence BuildResult debug logs — verbose in hot paths
logging.getLogger("pynixd.types.BuildResult").setLevel(logging.WARNING)


@dataclass
class KeyedBuildResult:
    """A build result associated with its derived path."""

    derived_path: derived_path_mod.DerivedPath = field(
        default_factory=lambda: StorePath("")
    )  # type: ignore
    result: BuildResult = field(default_factory=BuildResult)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.derived_path = await reader.read_string(derived_path_mod.DerivedPath)
        self.result = await BuildResult().from_reader(reader, version)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.derived_path)
        await self.result.to_writer(writer, version)


# ── Helpers ──────────────────────────────────────────────────────────


class ByteCollector(NixWriter):
    """NixWriter that collects bytes into a buffer."""

    def __init__(self, identifier: str = "memory") -> None:
        super().__init__(identifier=identifier)
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    async def drain(self) -> None:
        pass

    async def is_dirty(self) -> bool:
        return len(self._buf) > 0

    def getvalue(self) -> bytes:
        return bytes(self._buf)
