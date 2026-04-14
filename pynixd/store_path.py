"""
StorePath: a str subclass for Nix store paths with helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self


class StorePath(str):
    """A str subclass representing a Nix store path.

    Provides helpers for basename, derivation checking, etc.
    Can store optional 'extrainfo' for debugging (e.g. why this path is required).
    """

    __slots__ = ("extrainfo",)

    def __new__(cls, value: str | StorePath, extrainfo: Any = None) -> Self:
        instance = str.__new__(cls, value)
        # Use getattr if value is already a StorePath to preserve existing info if not overridden
        instance.extrainfo = extrainfo or getattr(value, "extrainfo", None)
        return instance

    @property
    def name(self) -> str:
        """The full basename (hash-name) of the store path."""
        return Path(self).name

    def hash_part(self) -> str:
        """The 32-character hash part of the store path."""
        return self.name.split("-", 1)[0]

    def base_name(self) -> str:
        """The human-readable name part (after the hash)."""
        parts = self.name.split("-", 1)
        return parts[1] if len(parts) > 1 else ""

    def is_derivation(self) -> bool:
        """Return True if this is a .drv path."""
        return self.endswith(".drv")

    def to_path(self) -> Path:
        """Convert to a pathlib.Path."""
        return Path(self)

    def __repr__(self) -> str:
        if self.extrainfo:
            return f"StorePath({str.__repr__(self)}, info={self.extrainfo!r})"
        return f"StorePath({str.__repr__(self)})"
