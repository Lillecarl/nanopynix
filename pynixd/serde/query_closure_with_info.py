"""QueryClosureWithInfo operation — WireRequest/WireResponse types."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field as PydanticField

from .store_path import StorePath  # noqa: TC001
from .valid_path_info import ValidPathInfo  # noqa: TC001
from .wire_ops import WireRequest, WireResponse


class QueryClosureWithInfoResponse(WireResponse):
    """QueryClosureWithInfo response — list of ValidPathInfo objects."""

    infos: list[ValidPathInfo] = PydanticField(default_factory=list)


class QueryClosureWithInfoRequest(WireRequest):
    """QueryClosureWithInfo request — set of seed StorePaths."""

    op: ClassVar[int] = 105
    is_extension: ClassVar[bool] = True
    response_type = QueryClosureWithInfoResponse
    paths: set[StorePath] = PydanticField(default_factory=set)
