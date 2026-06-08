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


class DrvOutput:
    """A Nix derivation output identifier.

    Format: ``<hash-algo>:<base16-hash>!<outputName>``
    Example: ``sha256:ba0770319c4c4c5f849e8e0e4a8b9c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8!out``

    The hash is the derivation's ``hashDerivationModulo`` — NOT the .drv store path.

    Can be constructed from a string or from keyword components::

        DrvOutput("sha256:abc!out")
        DrvOutput(hash_algo="sha256", hash_value="abc", output_name="out")
    """

    def __init__(
        self,
        value: str = "",
        *,
        hash_algo: str | None = None,
        hash_value: str | None = None,
        output_name: str | None = None,
        path: str = "",
    ) -> None:
        if isinstance(value, DrvOutput):
            self._hash_algo = value._hash_algo
            self._hash_value = value._hash_value
            self._output_name = value._output_name
            self._path = value._path
            return
        if hash_algo is not None:
            self._hash_algo = hash_algo
            self._hash_value = hash_value or ""
            self._output_name = output_name or ""
            self._path = path
        else:
            if value and "!" not in value:
                raise ValueError(
                    f"Invalid DrvOutput: {value!r} — expected format '<algo>:<hash>!<outputName>'",
                )
            if value:
                id_hash, self._output_name = value.split("!", 1)
                self._hash_algo, self._hash_value = id_hash.split(":", 1)
            else:
                self._hash_algo = ""
                self._hash_value = ""
                self._output_name = ""
            self._path = ""

    @property
    def hash_algo(self) -> str:
        return self._hash_algo

    @property
    def hash_value(self) -> str:
        return self._hash_value

    @property
    def output_name(self) -> str:
        return self._output_name

    @property
    def name(self) -> str:
        """Alias for ``output_name`` — matches the ``OutputInfo`` field."""
        return self._output_name

    @property
    def path(self) -> str:
        """The output store path, or empty for CA derivations."""
        return self._path

    @property
    def id_hash(self) -> str:
        """The hash-algorithm-prefixed hash part (before '!')."""
        return f"{self._hash_algo}:{self._hash_value}"

    def __str__(self) -> str:
        if not self._hash_algo and not self._hash_value and not self._output_name:
            return ""
        return f"{self._hash_algo}:{self._hash_value}!{self._output_name}"

    def __repr__(self) -> str:
        return f"DrvOutput({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DrvOutput):
            return NotImplemented
        return (
            self._hash_algo == other._hash_algo
            and self._hash_value == other._hash_value
            and self._output_name == other._output_name
        )

    def __hash__(self) -> int:
        return hash((self._hash_algo, self._hash_value, self._output_name))
