from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ConfigDict
from pydantic import Field as PydanticField

from .drv_output import DrvOutput  # noqa: TC001
from .store_path import StorePath  # noqa: TC001
from .wire_message import WireModel

if TYPE_CHECKING:
    from ..types.context import ReadContext, WriteContext


class Realisation(WireModel):
    """A Realisation on the Nix daemon wire.

    Wire format: length-prefixed UTF-8 containing a JSON object.
    Uses from_json/to_json for both wire and JSON serde, so Pydantic
    validation applies uniformly.
    """

    model_config = ConfigDict(validate_by_alias=True)

    id: DrvOutput | None = PydanticField(default=None)
    out_path: StorePath | None = PydanticField(default=None, alias="outPath")
    signatures: list[str] = PydanticField(default_factory=list)
    dependent_realisations: dict[str, str] = PydanticField(default_factory=dict, alias="dependentRealisations")

    @classmethod
    async def from_reader(cls, ctx: ReadContext):
        raw = await ctx.reader.read_string(str)
        return cls.from_json(raw)

    async def to_writer(self, ctx: WriteContext) -> None:
        ctx.writer.write_string(self.to_json())

    def to_json(self, **kwargs) -> str:
        kwargs.setdefault("by_alias", True)
        return self.model_dump_json(**kwargs)
