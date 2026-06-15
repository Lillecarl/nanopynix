"""BuildPathsWithResults operation — WireRequest/WireResponse types."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field as PydanticField

from .derived_path import DerivedPath  # noqa: TC001
from .keyed_build_result import KeyedBuildResult  # noqa: TC001
from .wire_ops import WireRequest, WireResponse


class BuildPathsWithResultsResponse(WireResponse):
    """BuildPathsWithResults response — list of KeyedBuildResults."""

    results: list[KeyedBuildResult] = PydanticField(default_factory=list)


class BuildPathsWithResultsRequest(WireRequest):
    """BuildPathsWithResults request — same wire format as BuildPaths.

    Wire fields: set[DerivedPath] then build_mode uint64.
    """

    op: ClassVar[int] = 46
    forward: ClassVar[bool] = False
    response_type = BuildPathsWithResultsResponse
    derived_paths: set[DerivedPath] = PydanticField(default_factory=set)
    build_mode: int
