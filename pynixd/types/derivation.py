"""Derivation and output domain models."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Self

from ..store_path import StorePath
from ..system_features import PYNIXD_HANDLED_FEATURES

if TYPE_CHECKING:
    from .aliases import StorePathSet
    from .context import ReadContext, WriteContext


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
    path: str
    method: str = ""
    hash_digest: str = ""

    @property
    def kind(self) -> OutputKind:
        """Classify this output based on wire protocol fields."""
        if self.method == "":
            if self.path == "":
                return OutputKind.DEFERRED
            return OutputKind.INPUT_ADDRESSED
        if self.hash_digest == "impure":
            return OutputKind.IMPURE
        if self.hash_digest != "":
            return OutputKind.CA_FIXED
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


@dataclass(kw_only=True)
class BasicDerivation:
    outputs: dict[str, DerivationOutput] = field(default_factory=dict)
    input_srcs: StorePathSet = field(default_factory=set)
    platform: str
    builder: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    is_dynamic: bool = field(default=False, repr=False)

    @property
    def requires_nix(self) -> bool:
        return not self.supports_lix()

    @property
    def build_local(self) -> bool:
        return self.env.get("pynixd_fast") == "1" or self.env.get("preferLocalBuild") == "1"

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

    def to_stats_json(self) -> str:
        return json.dumps(
            {
                "builder": self.builder,
                "outputs": list(self.outputs.keys()),
                "system": self.env.get("system", self.platform),
            },
            sort_keys=True,
        )

    @property
    def has_dynamic_outputs(self) -> bool:
        return any(o.is_dynamic_output for o in self.outputs.values())

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        n = await ctx.reader.read_uint64()
        obj.outputs = {}
        for _ in range(n):
            name = await ctx.reader.read_string()
            obj.outputs[name] = DerivationOutput(
                path=await ctx.reader.read_string(),
                method=await ctx.reader.read_string(),
                hash_digest=await ctx.reader.read_string(),
            )
        obj.input_srcs = await ctx.reader.read_string_set(StorePath)
        obj.platform = await ctx.reader.read_string()
        obj.builder = await ctx.reader.read_string()
        obj.args = await ctx.reader.read_string_list()
        n_env = await ctx.reader.read_uint64()
        obj.env = {}
        for _ in range(n_env):
            k = await ctx.reader.read_string()
            v = await ctx.reader.read_string()
            obj.env[k] = v
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        ctx.writer.write_uint64(len(self.outputs))
        for name, out in self.outputs.items():
            ctx.writer.write_string(name)
            # Normalize output paths to absolute store paths.
            # The drv_parser may return bare hash-names for CA derivations,
            # but the daemon protocol requires absolute paths.
            out_path = str(StorePath(out.path).with_store_prefix()) if out.path else ""
            ctx.writer.write_string(out_path)
            ctx.writer.write_string(out.method)
            ctx.writer.write_string(out.hash_digest)
        ctx.writer.write_string_set({src.with_store_prefix() for src in self.input_srcs})
        ctx.writer.write_string(self.platform)
        ctx.writer.write_string(self.builder)
        ctx.writer.write_string_list(self.args)
        ctx.writer.write_uint64(len(self.env))
        for k, v in sorted(self.env.items()):
            ctx.writer.write_string(k)
            ctx.writer.write_string(v)

    @property
    def has_ca_floating(self) -> bool:
        return any(o.is_floating_ca and not o.is_text_hashed for o in self.outputs.values())

    @property
    def has_deferred(self) -> bool:
        return any(o.is_deferred for o in self.outputs.values())

    @property
    def has_impure(self) -> bool:
        return any(o.is_impure for o in self.outputs.values())

    @property
    def has_text_hashed(self) -> bool:
        return any(o.is_text_hashed for o in self.outputs.values())
