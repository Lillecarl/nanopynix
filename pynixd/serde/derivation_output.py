from __future__ import annotations

from .wire_message import WireModel


class DerivationOutput(WireModel):
    """Wire mirror of DerivationOutput. Three string fields on the wire."""

    path: str
    method: str = ""
    hash_digest: str = ""
