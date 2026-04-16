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
from enum import Enum, IntEnum, auto
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar

import structlog

from .. import wire
from ..exceptions import OpNotImplementedError
from ..store_path import StorePath
from ..wire import NixReader, NixWriter

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..derived_path import DerivedPath
    from ..proxy import DaemonProxy
    from ..stderr import StderrError, StderrMsg
    from ..store import Store

log = structlog.get_logger(__name__)


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


class BuildMode(IntEnum):
    """Build mode flags from nix daemon protocol (see worker-protocol.cc)."""

    NORMAL = 0
    REPAIR = 1
    CHECK = 2


class Role(IntEnum):
    """Client authorization roles."""

    USER = 0
    ADMIN = 1


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
    logger: ClassVar[structlog.BoundLogger] = structlog.get_logger(
        f"pynixd.operations.{__name__}"
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Runs when an OpRequest subclass is instantiated, registers
        the subclass in OP_REGISTRY"""
        super().__init_subclass__(**kwargs)
        if "op" in cls.__dict__:
            OP_REGISTRY[cls.op] = cls
            cls.logger = structlog.get_logger(f"pynixd.operations.{cls.__name__}")

    @classmethod
    async def handle(cls, ctx: RequestContext) -> OpResponse | None:
        """Handle this operation from a client.

        Decodes the request and delegates execution to the stores.
        Streaming operations should override this method.
        """
        request = await cls.from_reader(ctx.proxy.r, ctx.version)
        result = await ctx.proxy.execute(request)
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
            if not store.probed:
                await store.probe_version()

            feature_name = type(self).name
            if feature_name in store.supported_features:
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

    @classmethod
    @abstractmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self: ...

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

    @classmethod
    async def from_reader(cls, reader: NixReader) -> Self:
        """Read stderr messages until STDERR_LAST from reader."""
        from ..stderr import read_stream

        logs = cls()
        async for msg in read_stream(reader):
            logs.add(msg)
        return logs


@dataclass
class OpResponse(ABC):
    """Base class for operation responses."""

    _log: ClassVar = structlog.get_logger(__name__)
    logger: ClassVar[structlog.BoundLogger] = structlog.get_logger(
        f"pynixd.operations.{__name__}"
    )
    logs: OperationLogs = field(default_factory=OperationLogs)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.logger = structlog.get_logger(f"pynixd.operations.{cls.__name__}")

    @property
    def is_not_found(self) -> bool:
        """True if this response indicates that the requested data was not found.
        Used by proxy to decide whether to try other stores.
        """
        return False

    @classmethod
    @abstractmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self: ...

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

    @classmethod
    async def from_reader(cls, reader: NixReader) -> Self:
        """Read UnkeyedValidPathInfo from wire."""
        return cls(
            deriver=await reader.read_string(StorePath),
            nar_hash=await reader.read_string(),
            references=await reader.read_string_set(StorePath),
            registration_time=await reader.read_uint64(),
            nar_size=await reader.read_uint64(),
            ultimate=await reader.read_uint64(),
            sigs=await reader.read_string_set(),
            ca=await reader.read_string(),
        )

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

    @classmethod
    async def from_reader(cls, reader: NixReader) -> Self:
        """Read ValidPathInfo (path + UnkeyedValidPathInfo) from wire."""
        path = await reader.read_string(StorePath)
        info = await UnkeyedValidPathInfo.from_reader(reader)
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
    outputs: dict[str, DerivationOutput] = field(default_factory=dict)
    input_srcs: set[StorePath] = field(default_factory=set)
    platform: str = ""
    builder: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Not part of wire protocol - set during conversion from ParsedDerivation
    is_dynamic: bool = field(default=False, repr=False)

    @property
    def requires_nix(self) -> bool:
        """True if this derivation needs nix (not lix)."""
        return not self.supports_lix()

    @property
    def build_local(self) -> bool:
        """True if this derivation should be built on the local store.

        Checks for explicit opt-in signals from the derivation author:
        - pynixd_fast=1 (pynixd-specific)
        - preferLocalBuild=1 (standard Nix attribute)
        """
        return (
            self.env.get("pynixd_fast") == "1"
            or self.env.get("preferLocalBuild") == "1"
        )

    def output_paths(self) -> dict[str, StorePath]:
        """Return {output_name: output_path} for all outputs."""
        return {name: StorePath(o.path) for name, o in self.outputs.items()}

    def serialize_for_stats(self) -> str:
        """Serialize derivation to a canonical string for stats matching.

        Includes builder, args, and a subset of stable environment variables.
        """
        # Exclude common noisy variables like out, bin, dev etc.
        # which change with every rebuild but don't affect complexity.
        noisy = {"out", "bin", "dev", "lib", "include", "man", "doc"}
        env_stable = {
            k: v
            for k, v in self.env.items()
            if k not in noisy and not k.startswith("NIX_")
        }
        parts = [
            f"B:{self.builder}",
            f"A:{' '.join(self.args)}",
            f"E:{json.dumps(env_stable, sort_keys=True)}",
        ]
        return "|".join(parts)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> BasicDerivation:
        n = await reader.read_uint64()
        outputs: dict[str, DerivationOutput] = {}
        for _ in range(n):
            name = await reader.read_string()
            outputs[name] = DerivationOutput(
                path=await reader.read_string(),
                method=await reader.read_string(),
                hash_digest=await reader.read_string(),
            )
        input_srcs = await reader.read_string_set(StorePath)
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
        for name, out in self.outputs.items():
            writer.write_string(name)
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
        if self.is_dynamic:
            return False
        for out in self.outputs.values():
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
        return any(
            o.is_floating_ca and not o.is_text_hashed for o in self.outputs.values()
        )

    @property
    def has_deferred(self) -> bool:
        """True if any output is deferred (depends on CA derivation)."""
        return any(o.is_deferred for o in self.outputs.values())

    @property
    def has_impure(self) -> bool:
        """True if any output is impure."""
        return any(o.is_impure for o in self.outputs.values())

    @property
    def has_text_hashed(self) -> bool:
        """True if any output uses text ingestion (any kind)."""
        return any(o.is_text_hashed for o in self.outputs.values())

    @property
    def has_dynamic_outputs(self) -> bool:
        """True if any output is text-hashed without pre-computed hash."""
        return any(o.is_dynamic_output for o in self.outputs.values())


@dataclass
class SubstitutablePathInfo:
    """Metadata for a substitutable path (missing but available)."""

    deriver: StorePath = field(default_factory=lambda: StorePath(""))
    references: set[StorePath] = field(default_factory=set)
    download_size: int = 0
    nar_size: int = 0

    @classmethod
    async def from_reader(
        cls, reader: NixReader, version: int
    ) -> SubstitutablePathInfo:
        return cls(
            deriver=await reader.read_string(StorePath),
            references=await reader.read_string_set(StorePath),
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


@dataclass
class BuildResult:
    _log: ClassVar = structlog.get_logger("pynixd.operations.BuildResult")
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
            "build_result_from_reader",
            status=status,
            error_msg=error_msg,
            built_outputs=built_outputs,
        )
        return result

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self._log.debug(
            "build_result_to_writer",
            status=self.status,
            error_msg=self.error_msg,
            built_outputs=self.built_outputs,
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
logging.getLogger("pynixd.operations.BuildResult").setLevel(logging.WARNING)


@dataclass
class KeyedBuildResult:
    """A build result associated with its derived path."""

    derived_path: DerivedPath = field(default_factory=lambda: StorePath(""))  # type: ignore
    result: BuildResult = field(default_factory=BuildResult)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        from ..derived_path import DerivedPath

        derived_path = await reader.read_string(DerivedPath)
        result = await BuildResult.from_reader(reader, version)
        return cls(derived_path=derived_path, result=result)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.derived_path)
        await self.result.to_writer(writer, version)


# ── Helpers ──────────────────────────────────────────────────────────


class ByteCollector(NixWriter):
    """NixWriter that collects bytes into a buffer."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    async def drain(self) -> None:
        pass

    async def is_dirty(self) -> bool:
        return len(self._buf) > 0

    def getvalue(self) -> bytes:
        return bytes(self._buf)
