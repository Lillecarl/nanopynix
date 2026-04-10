"""
DerivedPath: a str subclass for Nix derived path strings with .drv resolution helpers.

Format:
    /nix/store/xxx.drv!out          → specific output
    /nix/store/xxx.drv!out,lib      → multiple specific outputs
    /nix/store/xxx.drv!*            → all outputs (wildcard)

Bare .drv paths (without !) are normalized to !* on construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .store_path import StorePath

if TYPE_CHECKING:
    from .drv_parser import ParsedDerivation


class DerivedPath(StorePath):
    """A derived path string with helpers for .drv resolution.

    A DerivedPath is a StorePath subclass representing a Nix derived path,
    which encodes both the .drv file and the requested output names.
    """

    __slots__ = ()

    def __new__(cls, s: str) -> DerivedPath:
        # Normalize bare .drv paths to explicit wildcard form
        if s.endswith(".drv"):
            s = f"{s}!*"
        return super().__new__(cls, s)

    @property
    def drv_path(self) -> str:
        """The .drv store path (before any !), e.g. /nix/store/xxx.drv."""
        return self.split("!")[0]

    @property
    def output_names(self) -> set[str]:
        """The set of output names requested.

        Returns {"*"} for wildcard (all outputs).
        """
        outputs_str = self.split("!", 1)[1]
        if outputs_str == "*":
            return {"*"}
        return set(outputs_str.split(","))

    def to_derivation(self, store_path: Path) -> ParsedDerivation:
        """Read and parse the .drv file this derived path refers to.

        Args:
            store_path: The store root for reading the .drv file.

        Returns:
            ParsedDerivation for the .drv file.
        """
        from .drv_parser import read_drv_file

        return read_drv_file(store_path, self.drv_path)

    def to_outputs(self, store_path: Path) -> set[StorePath]:
        """Resolve this derived path to actual output store paths.

        Args:
            store_path: The store root for reading the .drv file.

        Returns:
            Set of store paths for the outputs requested by this derived path.
            Returns empty set if the .drv cannot be read.
        """
        names = self.output_names
        try:
            parsed = self.to_derivation(store_path)
        except (FileNotFoundError, OSError):
            return set()
        all_outputs = parsed.output_paths()  # {name: StorePath}
        if "*" in names:
            return set(all_outputs.values())
        return {p for n, p in all_outputs.items() if n in names}
