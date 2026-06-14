from __future__ import annotations

from .derivation_output import DerivationOutput  # noqa: TC001
from .store_path import StorePath  # noqa: TC001
from .wire_message import WireModel


class BasicDerivation(WireModel):
    """Wire mirror of BasicDerivation."""

    outputs: dict[str, DerivationOutput]
    input_srcs: set[StorePath]
    platform: str
    builder: str
    args: list[str]
    env: dict[str, str]
