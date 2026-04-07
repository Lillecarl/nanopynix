"""AddTempRoot operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ..protocol import Op
from .base import OpResponse, SingleStringRequest, Uint64Response

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class AddTempRootRequest(SingleStringRequest[Uint64Response]):
    op: ClassVar[int] = Op.AddTempRoot
    response_type: ClassVar[type[OpResponse]] = Uint64Response

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> Uint64Response:
        # We don't want clients creating temp roots on our host.
        return Uint64Response(value=0)
