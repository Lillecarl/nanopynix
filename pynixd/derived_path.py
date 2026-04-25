"""
DerivedPath and SingleDerivedPath: models for Nix derived path references.

Mirrors the Nix C++ types:
  SingleDerivedPath = Opaque(path) | Built(drv_path=SingleDerivedPath, output=str)
  DerivedPath        = Opaque(path) | Built(drv_path=SingleDerivedPath, outputs=OutputsSpec)

Wire format (daemon protocol): always uses the `!` separator (legacy format).
Public API format: uses the `^` separator.

Parsing follows Nix's right-to-left rfind strategy so that nested references
like `a.drv^out^out` are parsed as Built(Built(Opaque(a.drv), "out"), "out").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .drv_parser import read_drv_file
from .store_path import StorePath

if TYPE_CHECKING:
    from pathlib import Path

    from .drv_parser import ParsedDerivation


# ── OutputsSpec ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutputsAll:
    def to_string(self) -> str:
        return "*"


@dataclass(frozen=True)
class OutputsNames:
    names: frozenset[str]

    def to_string(self) -> str:
        return ",".join(sorted(self.names))


OutputsSpec = OutputsAll | OutputsNames


def _parse_outputs_spec(s: str) -> OutputsSpec:
    if s == "*":
        return OutputsAll()
    return OutputsNames(frozenset(s.split(",")))


# ── SingleDerivedPath ──────────────────────────────────────────────


@dataclass(frozen=True)
class SingleDerivedPathOpaque:
    path: StorePath

    def to_string(self) -> str:
        return str(self.path)

    def to_string_legacy(self) -> str:
        return str(self.path)

    def base_store_path(self) -> StorePath:
        return self.path


@dataclass(frozen=True)
class SingleDerivedPathBuilt:
    drv_path: SingleDerivedPath
    output: str

    def to_string(self) -> str:
        return f"{self.drv_path.to_string()}^{self.output}"

    def to_string_legacy(self) -> str:
        return f"{self.drv_path.to_string_legacy()}!{self.output}"

    def base_store_path(self) -> StorePath:
        return self.drv_path.base_store_path()


SingleDerivedPath = SingleDerivedPathOpaque | SingleDerivedPathBuilt


def _parse_single_derived_path(s: str, sep: str) -> SingleDerivedPath:
    n = s.rfind(sep)
    if n == -1:
        return SingleDerivedPathOpaque(path=StorePath(s))
    inner = _parse_single_derived_path(s[:n], sep)
    return SingleDerivedPathBuilt(drv_path=inner, output=s[n + 1 :])


# ── DerivedPath structured types (the union, NOT the str subclass below) ─


@dataclass(frozen=True)
class DerivedPathOpaque:
    path: StorePath

    def to_string(self) -> str:
        return str(self.path)

    def to_string_legacy(self) -> str:
        return str(self.path)

    def base_store_path(self) -> StorePath:
        return self.path


@dataclass(frozen=True)
class DerivedPathBuilt:
    drv_path: SingleDerivedPath
    outputs: OutputsSpec

    def to_string(self) -> str:
        return f"{self.drv_path.to_string()}^{self.outputs.to_string()}"

    def to_string_legacy(self) -> str:
        return f"{self.drv_path.to_string_legacy()}!{self.outputs.to_string()}"

    def base_store_path(self) -> StorePath:
        return self.drv_path.base_store_path()


DerivedPathUnion = DerivedPathOpaque | DerivedPathBuilt


# ── Parsing ────────────────────────────────────────────────────────


def _parse_derived_path(s: str, sep: str) -> DerivedPathUnion:
    n = s.rfind(sep)
    if n == -1:
        sp = StorePath(s)
        if sp.is_derivation():
            return DerivedPathBuilt(
                drv_path=SingleDerivedPathOpaque(path=sp),
                outputs=OutputsAll(),
            )
        return DerivedPathOpaque(path=sp)
    inner = _parse_single_derived_path(s[:n], sep)
    return DerivedPathBuilt(
        drv_path=inner,
        outputs=_parse_outputs_spec(s[n + 1 :]),
    )


def parse_derived_path(s: str) -> DerivedPathUnion:
    return _parse_derived_path(s, "^")


def parse_derived_path_legacy(s: str) -> DerivedPathUnion:
    return _parse_derived_path(s, "!")


# ── Helper accessors on DerivedPathUnion ────────────────────────────


def dp_drv_path(dp: DerivedPathUnion) -> str:
    if isinstance(dp, DerivedPathBuilt):
        return str(dp.base_store_path())
    return str(dp.path)


def dp_output_names(dp: DerivedPathUnion) -> set[str]:
    if isinstance(dp, DerivedPathBuilt):
        if isinstance(dp.outputs, OutputsAll):
            return {"*"}
        return set(dp.outputs.names)
    if dp.path.is_derivation():
        return {"*"}
    return set()


def dp_to_derivation(dp: DerivedPathUnion, store_path: Path) -> ParsedDerivation:
    return read_drv_file(store_path, dp_drv_path(dp))


def dp_to_outputs(dp: DerivedPathUnion, store_path: Path) -> set[StorePath]:
    names = dp_output_names(dp)
    try:
        parsed = dp_to_derivation(dp, store_path)
    except (FileNotFoundError, OSError):
        return set()
    all_outputs = parsed.output_paths()
    if "*" in names:
        return {p for p in all_outputs.values() if p != StorePath("")}
    return {p for n, p in all_outputs.items() if n in names and p != StorePath("")}


def dp_is_nested(dp: DerivedPathUnion) -> bool:
    if isinstance(dp, DerivedPathBuilt):
        return isinstance(dp.drv_path, SingleDerivedPathBuilt)
    return False


# ── DerivedPath (str subclass for wire compat) ─────────────────────


class DerivedPath(StorePath):
    """A str-subclass derived path wrapping the structured model.

    Preserves backward compatibility with wire protocol (read_string_set),
    while providing access to the structured representation via .derived.

    On the wire, uses the `!` separator (legacy format) matching the Nix
    daemon protocol.  Bare .drv paths are normalized to !* on construction.
    """

    __slots__ = ("_derived", "extrainfo")

    def __new__(cls, s: str) -> DerivedPath:
        derived = parse_derived_path_legacy(s)
        wire_str = derived.to_string_legacy()
        instance = str.__new__(cls, wire_str)
        instance._derived = derived
        instance.extrainfo = None
        return instance

    @property
    def derived(self) -> DerivedPathUnion:
        return self._derived

    @property
    def drv_path(self) -> str:
        return dp_drv_path(self._derived)

    @property
    def output_names(self) -> set[str]:
        return dp_output_names(self._derived)

    def to_derivation(
        self,
        store_path: Path,
        reader_fn: Any = None,
    ) -> ParsedDerivation:
        if reader_fn is not None:
            return reader_fn(store_path, self.drv_path)
        return dp_to_derivation(self._derived, store_path)

    def to_outputs(self, store_path: Path) -> set[StorePath]:
        return dp_to_outputs(self._derived, store_path)

    @property
    def is_nested(self) -> bool:
        return dp_is_nested(self._derived)
