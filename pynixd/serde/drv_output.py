from __future__ import annotations

from pydantic import ConfigDict
from pydantic import Field as PydanticField

from .wire_message import WireModel


class DrvOutput(WireModel):
    """DrvOutput on the wire — a JSON object inside Realisation.

    Nix wire uses camelCase keys.  Pydantic aliases handle the mapping.
    """

    model_config = ConfigDict(validate_by_alias=True, serialize_by_alias=True)

    drv_hash: str = PydanticField(alias="drvHash")
    output_name: str = PydanticField(alias="outputName")
