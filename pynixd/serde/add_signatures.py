"""AddSignatures operation — WireRequest/WireResponse types."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field as PydanticField

from .signature import Signature  # noqa: TC001
from .store_path import StorePath  # noqa: TC001
from .wire_ops import WireRequest, WireResponse


class AddSignaturesResponse(WireResponse):
    """AddSignatures response — single uint64 value."""

    value: int


class AddSignaturesRequest(WireRequest):
    """AddSignatures request — StorePath + set of Signatures."""

    op: ClassVar[int] = 37
    response_type = AddSignaturesResponse
    path: StorePath
    sigs: set[Signature] = PydanticField(default_factory=set)
