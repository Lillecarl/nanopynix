from __future__ import annotations

from typing import TYPE_CHECKING

from .wire_message import WireModel

if TYPE_CHECKING:
    from .basic_derivation import BasicDerivation
    from .build_result import BuildResult
    from .store_path import StorePath


class BuildDerivationResponse(WireModel):
    """BuildDerivation response — a BuildResult wrapped in the response body."""

    result: BuildResult


class BuildDerivationRequest(WireModel):
    """BuildDerivation request — wire format after the op code.

    Wire fields (in order):
      drv_path:   length-prefixed StorePath string
      derivation: BasicDerivation struct
      build_mode: uint64 (BuildMode int)
    """

    drv_path: StorePath
    derivation: BasicDerivation
    build_mode: int
