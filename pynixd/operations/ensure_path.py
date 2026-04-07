"""EnsurePath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..protocol import Op
from .base import OpResponse, SingleStringRequest, Uint64Response


@dataclass
class EnsurePathRequest(SingleStringRequest[Uint64Response]):
    op: ClassVar[int] = Op.EnsurePath
    response_type: ClassVar[type[OpResponse]] = Uint64Response
