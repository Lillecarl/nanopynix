"""Wire protocol message types for the Nix daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, model_serializer, model_validator
from pydantic import Field as PydanticField

from ..constants import proto
from .wire_message import WireField, WireMessage

if TYPE_CHECKING:
    from ..types.context import ReadContext, WriteContext


class WireString(WireMessage):
    """Base for wire types that serialize as a single string.

    Wire format:  [uint64 len][UTF-8 bytes]
    JSON format:  "the-string" (plain string, not object)

    Subclasses override _parse (post-read transform) and _format
    (pre-write transform). Auto-detected by WireMessage — no
    manual registration needed.
    """

    value: str

    # ── Override points ──

    @classmethod
    def _parse(cls, raw: str) -> str:
        """Transform raw wire value after reading. Default: identity."""
        return raw

    def _format(self) -> str:
        """Transform before writing to wire. Default: identity."""
        return self.value

    # ── Equality ──

    def __str__(self) -> str:
        return self.value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WireString):
            return self.value == other.value
        if isinstance(other, str):
            return self.value == other
        return NotImplemented

    # ── Binary serde — single string, delegated through _parse/_format ──

    @classmethod
    async def from_reader(cls, ctx: ReadContext):
        raw = await ctx.reader.read_string(str)
        return cls.model_construct(value=cls._parse(raw))

    async def to_writer(self, ctx: WriteContext) -> None:
        ctx.writer.write_string(self._format())

    # ── JSON serde — plain string ──

    @model_serializer
    def _ser(self) -> str:
        return self.value

    @model_validator(mode="before")
    @classmethod
    def _val(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"value": cls._parse(data)}
        if isinstance(data, cls):
            return data
        return data


class WireStorePath(WireString):
    """A store path — no custom parsing needed."""


class WireDrvOutput(WireMessage):
    """DrvOutput on the wire — a JSON object inside Realisation.

    Nix wire uses camelCase keys.  Pydantic aliases handle the mapping.
    """

    model_config = ConfigDict(populate_by_name=True)

    drv_hash: str = PydanticField(alias="drvHash")
    output_name: str = PydanticField(alias="outputName")


class WireRealisation(WireMessage):
    """A Realisation on the Nix daemon wire.

    Wire format: length-prefixed UTF-8 containing a JSON object.
    Uses from_json/to_json for both wire and JSON serde, so Pydantic
    validation applies uniformly.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: WireDrvOutput | None = PydanticField(default=None)
    out_path: WireStorePath | None = PydanticField(default=None, alias="outPath")
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


class WireOptMicroseconds(WireMessage):
    """Optional microseconds — [tag uint64][value uint64 if tag==1].

    tag=1 = present, tag=0 = absent.
    value is only on the wire when tag==1.
    """

    tag: int = 0
    value: int | None = WireField(default=None, wire_depends_on=lambda self: self.tag == 1)


class WireBuildResult(WireMessage):
    """Nix daemon protocol BuildResult.

    Fields present based on protocol version:
    - All versions: status, error_msg
    - >= 1.29: times_built, is_non_deterministic, start_time, stop_time
    - >= 1.37: cpu_user, cpu_system
    - >= 1.28: built_outputs (dict[str, str])
    """

    status: int
    error_msg: str

    # Protocol 1.29 fields
    times_built: int | None = WireField(default=None, min_version=proto(1, 29))
    is_non_deterministic: int | None = WireField(default=None, min_version=proto(1, 29))
    start_time: int | None = WireField(default=None, min_version=proto(1, 29))
    stop_time: int | None = WireField(default=None, min_version=proto(1, 29))

    # Protocol 1.37 fields
    cpu_user: WireOptMicroseconds = WireField(default_factory=WireOptMicroseconds, min_version=proto(1, 37))
    cpu_system: WireOptMicroseconds = WireField(default_factory=WireOptMicroseconds, min_version=proto(1, 37))

    # Protocol 1.28 fields
    built_outputs: dict[str, str] | None = WireField(default=None, min_version=proto(1, 28))


class WireBuildDerivationResponse(WireMessage):
    """BuildDerivation response — a BuildResult wrapped in the response body."""

    result: WireBuildResult


class WirePathInfo(WireMessage):
    """Wire mirror of UnkeyedValidPathInfo."""

    deriver: str
    nar_hash: str
    references: set[str]
    registration_time: int
    nar_size: int
    ultimate: int
    sigs: set[str]
    ca: str


class WireQueryPathInfoResponse(WireMessage):
    """QueryPathInfo response — info depends on valid flag."""

    valid: bool
    info: WirePathInfo | None = WireField(
        default=None,
        wire_depends_on=lambda self: self.valid,
    )
