"""QuerySubstitutablePaths operation request/response types."""

from __future__ import annotations

from typing import ClassVar

from ..protocol import Op
from .base import OpResponse, StringSetRequest, StringSetResponse


class QuerySubstitutablePathsRequest(StringSetRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QuerySubstitutablePaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True
