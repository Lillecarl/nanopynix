"""ProbeSystems operation — WireRequest/WireResponse types."""

from __future__ import annotations

from typing import ClassVar

from .wire_ops import WireRequest, WireResponse


class ProbeSystemsResponse(WireResponse):
    """ProbeSystems response — empty body, just stderr/WireLogs."""


class ProbeSystemsRequest(WireRequest):
    """ProbeSystems request — no body fields, internal operation."""

    op: ClassVar[int] = 108
    is_extension: ClassVar[bool] = True
    response_type = ProbeSystemsResponse
