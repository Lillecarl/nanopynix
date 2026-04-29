from __future__ import annotations

from typing import NotRequired, TypedDict


class Realisation(TypedDict):
    """Nix content-addressed derivation realisation."""

    id: str
    outPath: str
    signatures: NotRequired[list[str]]
