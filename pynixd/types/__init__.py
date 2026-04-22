"""
Shared types and enums for Nix daemon operations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum, IntEnum, auto
from typing import TYPE_CHECKING, ClassVar

import structlog

from ..store_path import DrvOutput, StorePath
from ..system_features import PYNIXD_HANDLED_FEATURES

if TYPE_CHECKING:
    from ..wire import NixReader, NixWriter


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
    def is_ca(self) -> bool:
        """True if this is any flavor of content-addressed."""
        return self.kind in (
            OutputKind.CA_FIXED,
            OutputKind.CA_FLOATING,
            OutputKind.IMPURE,
        )

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

    @property
    def required_system_features(self) -> set[str]:
        """Parse requiredSystemFeatures from the env dict."""
        raw = self.env.get("requiredSystemFeatures", "")
        if not raw:
            return set()
        return set(raw.split())

    @property
    def effective_required_features(self) -> set[str]:
        """Required features with pynixd-handled features stripped.

        Features like ca-derivations are resolved by pynixd before the
        build reaches the backend store. This property returns the
        feature set that the backend store actually needs to support.
        """

        return self.required_system_features - PYNIXD_HANDLED_FEATURES

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

    @property
    def has_dynamic_outputs(self) -> bool:
        """True if any output is text-hashed without pre-computed hash."""
        return any(o.is_dynamic_output for o in self.outputs.values())

    async def from_reader(self, reader: NixReader, version: int) -> BasicDerivation:
        n = await reader.read_uint64()
        self.outputs = {}
        for _ in range(n):
            name = await reader.read_string()
            self.outputs[name] = DerivationOutput(
                path=await reader.read_string(),
                method=await reader.read_string(),
                hash_digest=await reader.read_string(),
            )
        self.input_srcs = await reader.read_string_set(StorePath)
        self.platform = await reader.read_string()
        self.builder = await reader.read_string()
        self.args = await reader.read_string_list()
        n_env = await reader.read_uint64()
        self.env = {}
        for _ in range(n_env):
            k = await reader.read_string()
            v = await reader.read_string()
            self.env[k] = v
        return self

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
        for k, v in sorted(self.env.items()):
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


@dataclass
class BuildResult:
    """Result of a BuildDerivation or BuildPaths operation."""

    _log: ClassVar = structlog.get_logger("pynixd.types.BuildResult")
    status: BuildResultStatus = BuildResultStatus.BUILT
    error_msg: str = ""
    times_built: int = 0
    is_non_deterministic: int = 0
    start_time: int = 0
    stop_time: int = 0
    cpu_user: int | None = None
    cpu_system: int | None = None
    built_outputs: dict[DrvOutput, dict] = field(default_factory=dict)

    async def from_reader(self, reader: NixReader, version: int) -> BuildResult:
        from .. import wire  # Break circularity for proto()

        self.status = BuildResultStatus(await reader.read_uint64())
        self.error_msg = await reader.read_string()

        self.times_built = 0
        self.is_non_deterministic = 0
        self.start_time = 0
        self.stop_time = 0
        if version >= wire.proto(1, 29):
            self.times_built = await reader.read_uint64()
            self.is_non_deterministic = await reader.read_uint64()
            self.start_time = await reader.read_uint64()
            self.stop_time = await reader.read_uint64()

        self.cpu_user = None
        self.cpu_system = None
        if version >= wire.proto(1, 37):
            self.cpu_user = await reader.read_optional_uint64()
            self.cpu_system = await reader.read_optional_uint64()

        self.built_outputs = {}
        if version >= wire.proto(1, 28):
            n = await reader.read_uint64()
            for _ in range(n):
                drv_output = DrvOutput(await reader.read_string())
                realisation_json = await reader.read_string()
                self.built_outputs[drv_output] = json.loads(realisation_json)

        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        from .. import wire

        writer.write_uint64(self.status.value)
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
