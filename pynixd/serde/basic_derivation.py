from __future__ import annotations

from typing import TYPE_CHECKING

from .wire_message import WireModel

if TYPE_CHECKING:
    from .derivation_output import DerivationOutput
    from .store_path import StorePath


class BasicDerivation(WireModel):
    """Wire mirror of BasicDerivation."""

    outputs: dict[str, DerivationOutput]
    input_srcs: set[StorePath]
    platform: str
    builder: str
    args: list[str]
    env: dict[str, str]
