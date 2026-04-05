"""
StorePath: a str subclass for Nix store paths with helpers.
"""

from __future__ import annotations

from pathlib import Path


class StorePath(str):
    """A str subclass representing a Nix store path.

    Provides helpers for basename, derivation checking, etc.
    """

    __slots__ = ()

    @property
    def name(self) -> str:
        """The basename of the store path."""
        return Path(self).name

    def is_derivation(self) -> bool:
        """Return True if this is a .drv path."""
        return self.endswith(".drv")

    def to_path(self) -> Path:
        """Convert to a pathlib.Path."""
        return Path(self)
