"""QueryReferrers operation request/response types."""

from __future__ import annotations

from typing import ClassVar

from ..protocol import Op
from .base import OpResponse, SingleStringRequest, StringSetResponse


class QueryReferrersRequest(SingleStringRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryReferrers
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True
