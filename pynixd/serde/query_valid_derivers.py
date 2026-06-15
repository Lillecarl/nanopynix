"""QueryValidDerivers operation — WireRequest/WireResponse types."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field as PydanticField

from .store_path import StorePath  # noqa: TC001
from .wire_ops import WireRequest, WireResponse


class QueryValidDeriversResponse(WireResponse):
    """QueryValidDerivers response — set of deriver StorePaths."""

    paths: set[StorePath] = PydanticField(default_factory=set)


class QueryValidDeriversRequest(WireRequest):
    """QueryValidDerivers request — single StorePath to look up derivers for."""

    op: ClassVar[int] = 33
    response_type = QueryValidDeriversResponse
    path: StorePath
