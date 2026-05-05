"""Build result and status domain models."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import DrvOutput, StorePath

if TYPE_CHECKING:
    from ..derived_path import DerivedPath
    from ..wire import NixReader, NixWriter
    from .aliases import ContentAddress, NARHash
    from .ca import Realisation


class BuildResultStatus(IntEnum):
    """Build result status codes from nix daemon protocol."""

    BUILT = 0
    SUBSTITUTED = 1
    ALREADY_VALID = 2
    RESOLVES_TO_ALREADY_VALID = 13

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

    HASH_MISMATCH = 101


class BuildMode(IntEnum):
    """Build mode flags from nix daemon protocol."""

    NORMAL = 0
    REPAIR = 1
    CHECK = 2


@dataclass
class BuiltOutput:
    """A built output - either plain path or content-addressed (CA) format."""

    out_path: StorePath = field(default_factory=lambda: StorePath(""))
    ca: ContentAddress = ""
    hash: str = ""
    hash_algo: str = ""
    nar_hash: NARHash = ""
    nar_size: int = 0
    reference: str = ""

    @classmethod
    def from_string(cls, s: str) -> BuiltOutput:
        if not s:
            return cls()
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
        return cls(out_path=StorePath(s))

    def to_string(self) -> str:
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
        return self.out_path


@dataclass
class BuildResult:
    """Result of a BuildDerivation or BuildPaths operation."""

    logger: ClassVar = structlog.get_logger("pynixd.types.build.BuildResult")
    status: BuildResultStatus = BuildResultStatus.BUILT
    error_msg: str = ""
    times_built: int = 0
    is_non_deterministic: int = 0
    start_time: int = 0
    stop_time: int = 0
    cpu_user: int | None = None
    cpu_system: int | None = None
    built_outputs: dict[DrvOutput, Realisation] = field(default_factory=dict)

    async def from_reader(self, reader: NixReader, version: int) -> BuildResult:
        from .. import wire

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


@dataclass
class KeyedBuildResult:
    """A BuildResult together with its primary key (DerivedPath).

    Mirrors Nix's `KeyedBuildResult`: inherits BuildResult fields and adds
    the derivation path or store path that was built/substituted.
    """

    path: DerivedPath = field(default_factory=lambda: StorePath(""))  # type: ignore[assignment]  # always set by from_reader
    result: BuildResult = field(default_factory=BuildResult)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        from ..derived_path import DerivedPath

        self.path = await reader.read_string(DerivedPath)
        self.result = await BuildResult().from_reader(reader, version)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.path)
        await self.result.to_writer(writer, version)
