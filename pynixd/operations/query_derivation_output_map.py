"""QueryDerivationOutputMap operation request/response types."""

from __future__ import annotations

from typing import ClassVar

from ..protocol import Op
from .base import OpResponse, SingleStringRequest, StringMapResponse


class QueryDerivationOutputMapRequest(SingleStringRequest[StringMapResponse]):
    op: ClassVar[int] = Op.QueryDerivationOutputMap
    response_type: ClassVar[type[OpResponse]] = StringMapResponse
    is_query: ClassVar[bool] = True
