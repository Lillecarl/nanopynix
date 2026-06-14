from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field as PydanticField

from .wire_message import WireField, WireModel, WireResponse

if TYPE_CHECKING:
    from .nar_hash import NARHash
    from .signature import Signature
    from .store_path import StorePath
    from .unix_time import Time


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
