from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field as PydanticField

from .content_address import ContentAddress  # noqa: TC001

if TYPE_CHECKING:
    from .valid_path_info import ValidPathInfo
from .nar_hash import NARHash  # noqa: TC001
from .signature import Signature  # noqa: TC001
from .store_path import StorePath  # noqa: TC001
from .wire_message import WireModel
from .wire_time import Time  # noqa: TC001


class UnkeyedValidPathInfo(WireModel):
    """Wire mirror of UnkeyedValidPathInfo."""

    deriver: StorePath | None = PydanticField(default=None)
    nar_hash: NARHash
    references: set[StorePath]
    registration_time: Time
    nar_size: int
    ultimate: bool
    sigs: set[Signature]
    ca: ContentAddress

    def to_valid_path_info(self, path: StorePath) -> ValidPathInfo:
        """Wrap this UnkeyedValidPathInfo with a StorePath into ValidPathInfo."""
        from .valid_path_info import ValidPathInfo  # lazy to avoid circular

        return ValidPathInfo(path=path, info=self)
