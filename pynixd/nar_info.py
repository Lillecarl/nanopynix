"""NarInfo — full narinfo metadata from a Nix binary cache.

Richer than ``SubstitutablePathInfo``: captures URL, compression, NAR hash,
signatures, and all other fields needed for NAR downloading and store import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .store_path import StorePath

if TYPE_CHECKING:
    from .types import ValidPathInfo
    from .types.aliases import StorePathSet


@dataclass
class NarInfo:
    """Full metadata for a substitutable store path.

    Parsed from the ``.narinfo`` files served by Nix binary caches.
    Contains everything needed to download and verify a NAR.
    """

    store_path: StorePath
    url: str
    compression: str
    nar_hash: str
    nar_size: int
    references: StorePathSet = field(default_factory=set)
    deriver: StorePath = field(default_factory=lambda: StorePath(""))
    file_hash: str = ""
    file_size: int = 0
    system: str = ""
    ca: str = ""
    sigs: set[str] = field(default_factory=set)

    def to_valid_path_info(self) -> ValidPathInfo:
        """Convert to :class:`ValidPathInfo` for ``AddToStoreNar`` wire protocol."""
        from .types import ValidPathInfo

        return ValidPathInfo(
            path=self.store_path,
            nar_hash=self.nar_hash,
            nar_size=self.nar_size,
            references=self.references,
            deriver=self.deriver,
            ca=self.ca,
            sigs=self.sigs,
        )
