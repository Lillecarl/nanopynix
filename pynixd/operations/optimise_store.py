"""OptimiseStore operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..protocol import Op
from .base import EmptyRequest, OpResponse, Uint64Response


@dataclass
class OptimiseStoreRequest(EmptyRequest[Uint64Response]):
    op: ClassVar[int] = Op.OptimiseStore
    response_type: ClassVar[type[OpResponse]] = Uint64Response
