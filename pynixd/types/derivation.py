"""Derivation and output domain models."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from ..store_path import StorePath
from ..system_features import PYNIXD_HANDLED_FEATURES

if TYPE_CHECKING:
    from ..wire import NixReader, NixWriter


class OutputKind(Enum):
    """Classification of a single derivation output."""

    INPUT_ADDRESSED = auto()
    """Traditional input-addressed output (path provided, no hash_algo)."""

    CA_FIXED = auto()
    """Fixed content-addressed output (path + hash_algo + hash all provided)."""

    CA_FLOATING = auto()
    """Floating content-addressed output (path empty, hash_algo provided, hash empty)."""

    DEFERRED = auto()
    """Deferred input-addressed output (path empty, hash_algo empty, hash empty)."""

    IMPURE = auto()
    """Impure output (path empty, hash_algo provided, hash="impure")."""


@dataclass
class DerivationOutput:
    path: str = ""
    method: str = ""
    hash_digest: str = ""

    @property
    def kind(self) -> OutputKind:
        """Classify this output based on wire protocol fields."""
        if self.method == "":
            if self.path == "":
                return OutputKind.DEFERRED
            else:
                return OutputKind.INPUT_ADDRESSED
        else:
            if self.hash_digest == "impure":
                return OutputKind.IMPURE
            elif self.hash_digest != "":
                return OutputKind.CA_FIXED
            else:
                return OutputKind.CA_FLOATING

    @property
    def is_ca(self) -> bool:
        return self.kind in (
            OutputKind.CA_FIXED,
            OutputKind.CA_FLOATING,
            OutputKind.IMPURE,
        )

    @property
    def is_text_hashed(self) -> bool:
        return self.method.startswith("text:")

    @property
    def is_fixed_ca(self) -> bool:
        return self.kind == OutputKind.CA_FIXED

    @property
    def is_floating_ca(self) -> bool:
        return self.kind == OutputKind.CA_FLOATING

    @property
    def is_deferred(self) -> bool:
        return self.kind == OutputKind.DEFERRED

    @property
    def is_impure(self) -> bool:
        return self.kind == OutputKind.IMPURE

    @property
    def is_dynamic_output(self) -> bool:
        return self.is_text_hashed and self.hash_digest == ""


@dataclass
class BasicDerivation:
    outputs: dict[str, DerivationOutput] = field(default_factory=dict)
    input_srcs: set[StorePath] = field(default_factory=set)
    platform: str = ""
    builder: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    is_dynamic: bool = field(default=False, repr=False)

    @property
    def requires_nix(self) -> bool:
        return not self.supports_lix()

    @property
    def build_local(self) -> bool:
        return (
            self.env.get("pynixd_fast") == "1"
            or self.env.get("preferLocalBuild") == "1"
        )

    @property
    def required_system_features(self) -> set[str]:
        raw = self.env.get("requiredSystemFeatures", "")
        if not raw:
            return set()
        return set(raw.split())

    @property
    def effective_required_features(self) -> set[str]:
        return self.required_system_features - PYNIXD_HANDLED_FEATURES

    def supports_lix(self) -> bool:
        if self.is_dynamic:
            return False
        return all(not (o.is_ca or o.is_deferred) for o in self.outputs.values())

    def output_paths(self) -> dict[str, StorePath]:
        return {name: StorePath(o.path) for name, o in self.outputs.items()}

    def serialize_for_stats(self) -> str:
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

    @property
    def has_ca_floating(self) -> bool:
        return any(
            o.is_floating_ca and not o.is_text_hashed for o in self.outputs.values()
        )

    @property
    def has_deferred(self) -> bool:
        return any(o.is_deferred for o in self.outputs.values())

    @property
    def has_impure(self) -> bool:
        return any(o.is_impure for o in self.outputs.values())

    @property
    def has_text_hashed(self) -> bool:
        return any(o.is_text_hashed for o in self.outputs.values())
