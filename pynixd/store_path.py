"""
StorePath: a str subclass for Nix store paths with helpers.
DrvOutput: a str subclass for Nix derivation output identifiers.
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

    def with_store_prefix(self) -> StorePath:
        """Return a StorePath with the /nix/store/ prefix guaranteed.

        If the path already starts with /nix/store/, returns self unchanged.
        If it's a bare basename (e.g. hash-name), prepends /nix/store/.
        """
        if self.startswith("/nix/store/"):
            return self
        return StorePath(f"/nix/store/{self}", extrainfo=self.extrainfo)

    def __repr__(self) -> str:
        if self.extrainfo:
            return f"StorePath({str.__repr__(self)}, info={self.extrainfo!r})"
        return f"StorePath({str.__repr__(self)})"


class DrvOutput(str):
    """A str subclass representing a Nix DrvOutput identifier.

    Format: ``<hash-algo>:<base16-hash>!<outputName>``
    Example: ``sha256:ba0770319c4c4c5f849e8e0e4a8b9c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8!out``

    The hash is the derivation's ``hashDerivationModulo`` — NOT the .drv store path.
    """

    __slots__ = ()

    def __new__(cls, value: str | DrvOutput = "") -> Self:
        instance = str.__new__(cls, value)
        if value and "!" not in value:
            raise ValueError(
                f"Invalid DrvOutput: {value!r} — "
                "expected format '<algo>:<hash>!<outputName>'",
            )
        return instance

    @property
    def id_hash(self) -> str:
        """The hash-algorithm-prefixed hash part (before '!')."""
        return self.split("!", 1)[0]

    @property
    def output_name(self) -> str:
        """The output name (after '!')."""
        return self.split("!", 1)[1]

    def __repr__(self) -> str:
        return f"DrvOutput({str.__repr__(self)})"
