from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, PlainSerializer

from pynixd.store_path import DrvOutput, StorePath


def _coerce_storepath(v: object) -> StorePath:
    if isinstance(v, StorePath):
        return v
    if isinstance(v, str):
        return StorePath(v)
    raise ValueError(f"Cannot coerce {type(v).__name__} to StorePath")


def _coerce_drvoutput(v: object) -> DrvOutput:
    if isinstance(v, DrvOutput):
        return v
    if isinstance(v, str):
        return DrvOutput(v)
    raise ValueError(f"Cannot coerce {type(v).__name__} to DrvOutput")


_StorePathField = Annotated[
    StorePath,
    BeforeValidator(_coerce_storepath),
    PlainSerializer(lambda x: x.base(), return_type=str, when_used="json"),
]
_DrvOutputField = Annotated[
    DrvOutput,
    BeforeValidator(_coerce_drvoutput),
    PlainSerializer(lambda x: str(x), return_type=str, when_used="json"),
]


class Realisation(BaseModel):
    """Nix content-addressed derivation realisation."""

    model_config = {
        "extra": "ignore",
        "arbitrary_types_allowed": True,
        "populate_by_name": True,
    }

    id: _DrvOutputField
    out_path: _StorePathField = Field(alias="outPath")
    signatures: list[str] = []
    dependent_realisations: dict[_DrvOutputField, _StorePathField] = Field(default={}, alias="dependentRealisations")
