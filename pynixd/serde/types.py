"""Wire protocol message types for the Nix daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ConfigDict
from pydantic import Field as PydanticField

from ..constants import proto
from .wire_message import WireField, WireMessage

if TYPE_CHECKING:
    from ..types.context import ReadContext, WriteContext


class WireString(WireMessage):
    """Abstract base: a single length-prefixed string on the wire.

    Subclasses define their own fields.  JSON is native Pydantic
    (the destructed object).  Wire format is ``str(self)``.

    ``to_writer`` writes ``str(self)`` as a single wire string.
    ``__hash__`` / ``__eq__`` delegate to ``str(self)``.
    """

    async def to_writer(self, ctx: WriteContext) -> None:
        ctx.writer.write_string(str(self))

    def __str__(self) -> str:
        raise NotImplementedError(f"{type(self).__name__} must override __str__")

    def __hash__(self) -> int:
        return hash(str(self))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WireString):
            return str(self) == str(other)
        if isinstance(other, str):
            return str(self) == other
        return NotImplemented


class WireStorePath(WireString):
    """A store path — single string field."""

    path: str

    def __str__(self) -> str:
        return self.path


class WireSignature(WireString):
    """A Nix signature — "name:signature" on the wire."""

    name: str = ""
    signature: str = ""

    @classmethod
    async def from_reader(cls, ctx: ReadContext):
        raw = await ctx.reader.read_string(str)
        parts = raw.split(":", 1)
        return cls.model_construct(
            name=parts[0],
            signature=parts[1] if len(parts) > 1 else "",
        )

    def __str__(self) -> str:
        return f"{self.name}:{self.signature}"

    @classmethod
    def from_parts(cls, name: str, sig: str) -> WireSignature:
        return cls(name=name, signature=sig)


class WireDrvOutput(WireMessage):
    """DrvOutput on the wire — a JSON object inside Realisation.

    Nix wire uses camelCase keys.  Pydantic aliases handle the mapping.
    """

    model_config = ConfigDict(validate_by_alias=True, serialize_by_alias=True)

    drv_hash: str = PydanticField(alias="drvHash")
    output_name: str = PydanticField(alias="outputName")


class WireRealisation(WireMessage):
    """A Realisation on the Nix daemon wire.

    Wire format: length-prefixed UTF-8 containing a JSON object.
    Uses from_json/to_json for both wire and JSON serde, so Pydantic
    validation applies uniformly.
    """

    model_config = ConfigDict(validate_by_alias=True)

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
