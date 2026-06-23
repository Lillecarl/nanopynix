"""ProbeFeatures operation — WireRequest/WireResponse types."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field as PydanticField

from .wire_ops import WireRequest, WireResponse


class ProbeFeaturesResponse(WireResponse):
    """ProbeFeatures response — supported features by system."""

    feature_matrix: dict[str, set[str]] = PydanticField(default_factory=dict)


class ProbeFeaturesRequest(WireRequest):
    """ProbeFeatures request — candidate systems and features to probe."""

    op: ClassVar[int] = 109
    response_type = ProbeFeaturesResponse
    systems: set[str] = PydanticField(default_factory=set)
    system_features: set[str] = PydanticField(default_factory=set)
