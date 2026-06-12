"""Wire protocol message types for the Nix daemon."""

from __future__ import annotations

from typing import Any

from pydantic import model_serializer, model_validator

from ..constants import proto
from .wire_message import WireField, WireMessage


class WireStorePath(WireMessage):
    """A store path on the Nix daemon wire protocol.

    Wire format: single length-prefixed UTF-8 string.

    Usage::

        class MyRequest(WireMessage):
            path: WireStorePath  # auto-detected as WireMessage subtype
    """

    path: str

    def __str__(self) -> str:
        return self.path

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WireStorePath):
            return NotImplemented
        return self.path == other.path

    @model_serializer
    def ser_model(self) -> str:
        """Serialize WireStorePath as a plain string in JSON."""
        return self.path

    @model_validator(mode="before")
    @classmethod
    def from_str(cls, data: Any) -> Any:
        """Deserialize WireStorePath from a plain string in JSON."""
        if isinstance(data, str):
            return {"path": data}
        if isinstance(data, cls):
            return data
        return data


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
