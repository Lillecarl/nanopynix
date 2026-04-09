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


class RequiredInput(StorePath):
    """A StorePath subclass that tracks where it was required from.

    For debugging: __repr__ shows the path AND the source.
    Uses a class-level dict to track sources (keyed by path string).
    """

    _sources: dict[str, str] = {}

    def __new__(cls, path: StorePath | str, source: str) -> "RequiredInput":
        instance = str.__new__(cls, path)
        cls._sources[str(path)] = source
        return instance

    @property
    def source(self) -> str:
        return self._sources.get(str(self), "")

    def __repr__(self) -> str:
        return f"RequiredInput({str.__repr__(self)}, source={self.source!r})"

    def to_store_path(self) -> StorePath:
        return StorePath(str(self))
