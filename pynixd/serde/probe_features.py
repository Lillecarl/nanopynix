"""ProbeFeatures operation — WireRequest/WireResponse types."""

from __future__ import annotations

from typing import ClassVar

from .wire_ops import WireRequest, WireResponse


class ProbeFeaturesResponse(WireResponse):
    """ProbeFeatures response — empty body, just stderr/WireLogs."""


class ProbeFeaturesRequest(WireRequest):
    """ProbeFeatures request — no body fields, internal operation."""

    op: ClassVar[int] = 109
    response_type = ProbeFeaturesResponse
