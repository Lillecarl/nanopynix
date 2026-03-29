"""
Base classes and common types for nix daemon operation serialization.

Each operation has a request and response dataclass with factory methods:
- from_reader(reader) — reads wire data into a Python object
- to_writer(writer)   — writes the Python object back to wire

These are the canonical way to speak the daemon protocol everywhere:
proxy, store, backend, scheduler, etc.
"""

from __future__ import annotations

import io
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum, Enum, auto
from typing import Any, ClassVar, Generic, Self, TypeVar

from .. import wire
from ..wire import NixReader, NixWriter

log = logging.getLogger(__name__)


class BuildResultStatus(IntEnum):
    """Build result status codes from nix daemon protocol.

    Values match the wire protocol (see common-protocol.cc):
    - 0-2 and 13 are success statuses
    - 3-12 and 14 are failure statuses
    - HashMismatch (not in wire protocol) is converted to OutputRejected
    """

    # Success statuses
    BUILT = 0
    SUBSTITUTED = 1
    ALREADY_VALID = 2
    RESOLVES_TO_ALREADY_VALID = 13

    # Failure statuses
    PERMANENT_FAILURE = 3
    INPUT_REJECTED = 4
    OUTPUT_REJECTED = 5
    TRANSIENT_FAILURE = 6
    CACHED_FAILURE = 7
    TIMED_OUT = 8
    MISC_FAILURE = 9
    DEPENDENCY_FAILED = 10
    LOG_LIMIT_EXCEEDED = 11
    NOT_DETERMINISTIC = 12
    NO_SUBSTITUTERS = 14

    # HashMismatch is not in the wire protocol; it's converted to OutputRejected
    # before serialization. Included here for completeness.
    HASH_MISMATCH = 101  # Internal only, not a wire value


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
    response_type: ClassVar[type[OpResponse]]
    is_query: ClassVar[bool] = False
    is_build: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "op" in cls.__dict__:
            OP_REGISTRY[cls.op] = cls
            # Each operation gets its own logger for fine-grained control
            cls._log: logging.Logger = logging.getLogger(
                f"pynixd.operations.{cls.__name__}"
            )

    @classmethod
    @abstractmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self: ...

    @abstractmethod
    async def to_writer(self, writer: NixWriter, version: int) -> None: ...


@dataclass
class OpResponse(ABC):
    """Base class for operation responses."""

    _log: ClassVar[logging.Logger] = logging.getLogger(__name__)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._log = logging.getLogger(f"pynixd.operations.{cls.__name__}")

    @classmethod
    @abstractmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self: ...

    @abstractmethod
    async def to_writer(self, writer: NixWriter, version: int) -> None: ...


# ── Common request types ─────────────────────────────────────────────


@dataclass
class EmptyRequest(OpRequest[Resp]):
    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls()

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        pass


@dataclass
class SingleStringRequest(OpRequest[Resp]):
    path: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(path=await reader.read_string())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.path)


@dataclass
class StringSetRequest(OpRequest[Resp]):
    paths: set[str] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)


@dataclass
class StringMapRequest(OpRequest[Resp]):
    items: dict[str, str] = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        items: dict[str, str] = {}
        for _ in range(n):
            k = await reader.read_string()
            v = await reader.read_string()
            items[k] = v
        return cls(items=items)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            writer.write_string(k)
            writer.write_string(v)


# ── Common response types ────────────────────────────────────────────


@dataclass
class EmptyResponse(OpResponse):
    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls()

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        pass


@dataclass
class Uint64Response(OpResponse):
    value: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(value=await reader.read_uint64())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.value)


@dataclass
class StringSetResponse(OpResponse):
    paths: set[str] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)


@dataclass
class SingleStringResponse(OpResponse):
    value: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(value=await reader.read_string())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.value)


@dataclass
class StringMapResponse(OpResponse):
    items: dict[str, str] = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        items: dict[str, str] = {}
        for _ in range(n):
            k = await reader.read_string()
            v = await reader.read_string()
            items[k] = v
        return cls(items=items)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.items))
        for k, v in self.items.items():
            writer.write_string(k)
            writer.write_string(v)


# ── Complex structures ───────────────────────────────────────────────


@dataclass
class PathInfo:
    """Metadata for a store path."""

    path: str = ""
    deriver: str = ""
    nar_hash: str = ""
    references: set[str] = field(default_factory=set)
    registration_time: int = 0
    nar_size: int = 0
    ultimate: int = 0
    sigs: set[str] = field(default_factory=set)
    ca: str = ""

    @classmethod
    async def from_reader_unkeyed(cls, reader: NixReader, path: str = "") -> PathInfo:
        """Read UnkeyedValidPathInfo (no path prefix) from wire."""
        return cls(
            path=path,
            deriver=await reader.read_string(),
            nar_hash=await reader.read_string(),
            references=await reader.read_string_set(),
            registration_time=await reader.read_uint64(),
            nar_size=await reader.read_uint64(),
            ultimate=await reader.read_uint64(),
            sigs=await reader.read_string_set(),
            ca=await reader.read_string(),
        )

    @classmethod
    async def from_reader_keyed(cls, reader: NixReader) -> PathInfo:
        """Read ValidPathInfo (path + UnkeyedValidPathInfo) from wire."""
        path = await reader.read_string()
        return await cls.from_reader_unkeyed(reader, path)

    async def to_writer_unkeyed(self, writer: NixWriter) -> None:
        """Write UnkeyedValidPathInfo (no path prefix) to wire."""
        writer.write_string(self.deriver)
        writer.write_string(self.nar_hash)
        writer.write_string_set(self.references)
        writer.write_uint64(self.registration_time)
        writer.write_uint64(self.nar_size)
        writer.write_uint64(self.ultimate)
        writer.write_string_set(self.sigs)
        writer.write_string(self.ca)

    async def to_writer_keyed(self, writer: NixWriter) -> None:
        """Write ValidPathInfo (path + UnkeyedValidPathInfo) to wire."""
        writer.write_string(self.path)
        await self.to_writer_unkeyed(writer)


class OutputKind(Enum):
    """Classification of a single derivation output."""

    INPUT_ADDRESSED = auto()
    """Traditional input-addressed output (path provided, no hash_algo)."""

    CA_FIXED = auto()
    """Fixed content-addressed output (path + hash_algo + hash all provided)."""

    CA_FLOATING = auto()
    """Floating content-addressed output (path empty, hash_algo provided, hash empty).
    Output path is determined at build time based on content.
    Requires CaDerivations experimental feature."""

    DEFERRED = auto()
    """Deferred input-addressed output (path empty, hash_algo empty, hash empty).
    Depends on a CA derivation whose output isn't known yet.
    Requires CaDerivations experimental feature."""

    IMPURE = auto()
    """Impure output (path empty, hash_algo provided, hash="impure").
    Always rebuilt, content-addressed location.
    Requires ImpureDerivations experimental feature."""


@dataclass
class DerivationOutput:
    name: str = ""
    path: str = ""
    method: str = ""
    hash_digest: str = ""

    @property
    def kind(self) -> OutputKind:
        """Classify this output based on wire protocol fields."""
        if self.method == "":
            # No hash algorithm - traditional or deferred
            if self.path == "":
                return OutputKind.DEFERRED
            else:
                return OutputKind.INPUT_ADDRESSED
        else:
            # Has hash algorithm
            if self.hash_digest == "impure":
                return OutputKind.IMPURE
            elif self.hash_digest != "":
                return OutputKind.CA_FIXED
            else:
                return OutputKind.CA_FLOATING

    @property
    def is_text_hashed(self) -> bool:
        """True if this uses text ingestion (method starts with 'text:')."""
        return self.method.startswith("text:")

    @property
    def is_fixed_ca(self) -> bool:
        """True if this is fixed content-addressed (CAFixed)."""
        return self.kind == OutputKind.CA_FIXED

    @property
    def is_floating_ca(self) -> bool:
        """True if this is floating content-addressed (CAFloating)."""
        return self.kind == OutputKind.CA_FLOATING

    @property
    def is_deferred(self) -> bool:
        """True if this is deferred (depends on CA derivation)."""
        return self.kind == OutputKind.DEFERRED

    @property
    def is_impure(self) -> bool:
        """True if this is impure."""
        return self.kind == OutputKind.IMPURE

    @property
    def is_dynamic_output(self) -> bool:
        """True if this is text-hashed without pre-computed hash.

        Text-hashed outputs where the hash isn't known at derivation parse time
        are a special case of CAFloating that additionally requires
        DynamicDerivations experimental feature.
        """
        return self.is_text_hashed and self.hash_digest == ""


@dataclass
class BasicDerivation:
    outputs: list[DerivationOutput] = field(default_factory=list)
    input_srcs: set[str] = field(default_factory=set)
    platform: str = ""
    builder: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Not part of wire protocol - set during conversion from ParsedDerivation
    _is_dynamic: bool = field(default=False, repr=False)

    @property
    def requires_nix(self) -> bool:
        """True if this derivation needs nix (not lix)."""
        return not self.supports_lix()

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> BasicDerivation:
        n = await reader.read_uint64()
        outputs = []
        for _ in range(n):
            outputs.append(
                DerivationOutput(
                    name=await reader.read_string(),
                    path=await reader.read_string(),
                    method=await reader.read_string(),
                    hash_digest=await reader.read_string(),
                )
            )
        input_srcs = await reader.read_string_set()
        platform = await reader.read_string()
        builder = await reader.read_string()
        args = await reader.read_string_list()
        n_env = await reader.read_uint64()
        env: dict[str, str] = {}
        for _ in range(n_env):
            k = await reader.read_string()
            v = await reader.read_string()
            env[k] = v
        return cls(
            outputs=outputs,
            input_srcs=input_srcs,
            platform=platform,
            builder=builder,
            args=args,
            env=env,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.outputs))
        for out in self.outputs:
            writer.write_string(out.name)
            writer.write_string(out.path)
            writer.write_string(out.method)
            writer.write_string(out.hash_digest)
        writer.write_string_set(self.input_srcs)
        writer.write_string(self.platform)
        writer.write_string(self.builder)
        writer.write_string_list(self.args)
        writer.write_uint64(len(self.env))
        for k, v in self.env.items():
            writer.write_string(k)
            writer.write_string(v)

    def supports_lix(self) -> bool:
        """True if this derivation can be handled by a Lix backend.

        Lix supports:
        - Traditional derivations (InputAddressed outputs)
        - CAFixed outputs (fixed CA with known hash)

        Lix does NOT support:
        - DrvWithVersion("xp-dyn-drv") format (dynamic derivations)
        - CAFloating outputs (floating CA without known hash, not text-hashed)
        - Deferred outputs (depends on CA derivation)
        - Impure outputs
        - Text-hashed outputs without pre-computed hash (dynamic outputs)
        """
        if self._is_dynamic:
            return False
        for out in self.outputs:
            kind = out.kind
            if kind == OutputKind.DEFERRED:
                return False
            if kind == OutputKind.IMPURE:
                return False
            if kind == OutputKind.CA_FLOATING and not out.is_text_hashed:
                return False
            # Text-hashed with known hash is CAFixed, which is fine
            # Text-hashed without hash is CAFloating + DynamicDerivations, not supported
        return True

    @property
    def has_ca_floating(self) -> bool:
        """True if any output is floating CA (CAFloating, not text-hashed)."""
        return any(o.is_floating_ca and not o.is_text_hashed for o in self.outputs)

    @property
    def has_deferred(self) -> bool:
        """True if any output is deferred (depends on CA derivation)."""
        return any(o.is_deferred for o in self.outputs)

    @property
    def has_impure(self) -> bool:
        """True if any output is impure."""
        return any(o.is_impure for o in self.outputs)

    @property
    def has_text_hashed(self) -> bool:
        """True if any output uses text ingestion (any kind)."""
        return any(o.is_text_hashed for o in self.outputs)

    @property
    def has_dynamic_outputs(self) -> bool:
        """True if any output is text-hashed without pre-computed hash."""
        return any(o.is_dynamic_output for o in self.outputs)


@dataclass
class SubstPathInfo:
    """Substitutable path info (deriver, refs, sizes)."""

    deriver: str = ""
    references: set[str] = field(default_factory=set)
    download_size: int = 0
    nar_size: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> SubstPathInfo:
        return cls(
            deriver=await reader.read_string(),
            references=await reader.read_string_set(),
            download_size=await reader.read_uint64(),
            nar_size=await reader.read_uint64(),
        )

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

    out_path: str = ""
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
                    out_path=data.get("outPath", ""),
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
        return cls(out_path=s)

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


@dataclass
class BuildResult:
    _log: ClassVar[logging.Logger] = logging.getLogger("pynixd.operations.BuildResult")
    status: BuildResultStatus = BuildResultStatus.BUILT
    error_msg: str = ""
    times_built: int = 0
    is_non_deterministic: int = 0
    start_time: int = 0
    stop_time: int = 0
    cpu_user: int | None = None
    cpu_system: int | None = None
    # built_outputs maps DrvOutput -> Realisation (parsed JSON dict)
    built_outputs: dict[str, dict] = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> BuildResult:
        status = BuildResultStatus(await reader.read_uint64())
        error_msg = await reader.read_string()

        times_built = 0
        is_non_deterministic = 0
        start_time = 0
        stop_time = 0
        if version >= wire.proto(1, 29):
            times_built = await reader.read_uint64()
            is_non_deterministic = await reader.read_uint64()
            start_time = await reader.read_uint64()
            stop_time = await reader.read_uint64()

        cpu_user: int | None = None
        cpu_system: int | None = None
        if version >= wire.proto(1, 37):
            cpu_user = await reader.read_optional_uint64()
            cpu_system = await reader.read_optional_uint64()

        built_outputs: dict[str, dict] = {}
        if version >= wire.proto(1, 28):
            n = await reader.read_uint64()
            for _ in range(n):
                drv_output = await reader.read_string()
                realisation_json = await reader.read_string()
                built_outputs[drv_output] = json.loads(realisation_json)

        result = cls(
            status=status,
            error_msg=error_msg,
            times_built=times_built,
            is_non_deterministic=is_non_deterministic,
            start_time=start_time,
            stop_time=stop_time,
            cpu_user=cpu_user,
            cpu_system=cpu_system,
            built_outputs=built_outputs,
        )
        cls._log.debug(
            "BuildResult from_reader: status=%d, error='%s', outputs=%s",
            status,
            error_msg,
            built_outputs,
        )
        return result

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._log.debug(
            "BuildResult to_writer: status=%d, error='%s', outputs=%s",
            self.status,
            self.error_msg,
            self.built_outputs,
        )
        writer.write_uint64(self.status)
        writer.write_string(self.error_msg)

        if version >= wire.proto(1, 29):
            writer.write_uint64(self.times_built)
            writer.write_uint64(self.is_non_deterministic)
            writer.write_uint64(self.start_time)
            writer.write_uint64(self.stop_time)

        if version >= wire.proto(1, 37):
            writer.write_optional_uint64(self.cpu_user)
            writer.write_optional_uint64(self.cpu_system)

        if version >= wire.proto(1, 28):
            writer.write_uint64(len(self.built_outputs))
            for k, v in self.built_outputs.items():
                writer.write_string(k)
                writer.write_string(json.dumps(v))


# Silence BuildResult debug logs — verbose in hot paths
BuildResult._log.setLevel(logging.WARNING)


# ── Helpers ──────────────────────────────────────────────────────────


class ByteCollector(NixWriter):
    """NixWriter that collects bytes into a buffer."""

    def __init__(self) -> None:
        self._buf = io.BytesIO()

    def write(self, data: bytes) -> None:
        self._buf.write(data)

    async def drain(self) -> None:
        pass

    def getvalue(self) -> bytes:
        return self._buf.getvalue()
