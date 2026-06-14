from __future__ import annotations

from pydantic import Field as PydanticField

from .nar_hash import NARHash  # noqa: TC001
from .signature import Signature  # noqa: TC001
from .store_path import StorePath  # noqa: TC001
from .unix_time import Time  # noqa: TC001
from .wire_message import WireField, WireModel
from .wire_ops import WireResponse


class UnkeyedValidPathInfo(WireModel):
    """Wire mirror of UnkeyedValidPathInfo."""

    deriver: StorePath | None = PydanticField(default=None)
    nar_hash: NARHash
    references: set[StorePath]
    registration_time: Time
    nar_size: int
    ultimate: int
    sigs: set[Signature]
    ca: str


class QueryPathInfoResponse(WireResponse):
    """QueryPathInfo response — info depends on valid flag."""

    valid: bool
    info: UnkeyedValidPathInfo | None = WireField(
        default=None,
        wire_depends_on=lambda self: self.valid,
    )
