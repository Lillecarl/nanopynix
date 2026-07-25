"""Data types for all nanopynix values crossing the C++/Python boundary.

Most types are re-exported from ``nanopynix_proto.nix.common`` — the
proto-generated messages are the canonical wire format.  A few types use
extension subclasses to add helper methods that the proto generated code
doesn't provide (``is_derivation``, ``message``, etc.).

Shared wire-protocol *constants* (route strings, env var names, handle-kind
tags) live in ``nanopynix._wire`` instead -- a different concern from the
data types here, despite both crossing the same process boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nanopynix_proto.nix.common import (
    # Types with extension subclasses — imported as private for subclassing
    BuildResult as BuildResult,
)
from nanopynix_proto.nix.common import (
    CallArg as CallArg,
)
from nanopynix_proto.nix.common import (
    CallArgAttrs as CallArgAttrs,
)
from nanopynix_proto.nix.common import (
    CallArgList as CallArgList,
)
from nanopynix_proto.nix.common import (
    DeepAttrs as DeepAttrs,
)
from nanopynix_proto.nix.common import (
    DeepList as DeepList,
)
from nanopynix_proto.nix.common import (
    DeepValue as DeepValue,
)
from nanopynix_proto.nix.common import (
    Derivation as Derivation,
)
from nanopynix_proto.nix.common import (
    DerivationOutputs as DerivationOutputs,
)
from nanopynix_proto.nix.common import (
    FlakeRef as FlakeRef,
)
from nanopynix_proto.nix.common import (
    Input as Input,
)
from nanopynix_proto.nix.common import (
    LockedFlake as LockedFlake,
)
from nanopynix_proto.nix.common import (
    LockedInput as LockedInput,
)
from nanopynix_proto.nix.common import (
    LogEvent as _LogEventProto,
)
from nanopynix_proto.nix.common import (
    MissingInfo as MissingInfo,
)
from nanopynix_proto.nix.common import (
    NixLogEvent as NixLogEvent,
)
from nanopynix_proto.nix.common import (
    NixType as NixType,
)
from nanopynix_proto.nix.common import (
    NullValue as NullValue,
)
from nanopynix_proto.nix.common import (
    PathInfo as PathInfo,
)
from nanopynix_proto.nix.common import (
    PrimOpSpec as PrimOpSpec,
)
from nanopynix_proto.nix.common import (
    RemoteCallArg as RemoteCallArg,
)
from nanopynix_proto.nix.common import (
    ResultType as ResultType,
)
from nanopynix_proto.nix.common import (
    ScalarValue as ScalarValue,
)
from nanopynix_proto.nix.common import (
    ValueHandle as ValueHandle,
)
from strip_ansi import (  # type: ignore[reportMissingTypeStubs] -- strip_ansi has no PEP 561 stubs
    strip_ansi as _strip_ansi,
)


# ══════════════════════════════════════════════════════════════════════════
class StorePath(str):
    """A store path string with parsed Nix store-path properties."""

    __slots__ = ()

    HashLen = 32
    MaxPathLen = 211

    def __new__(cls, value: str) -> StorePath:
        if isinstance(value, cls):
            return value
        return super().__new__(cls, value)

    @property
    def base_name(self) -> str:
        """The final path component of this store path."""
        return self.rstrip("/").rsplit("/", 1)[-1]

    @property
    def hash_part(self) -> str:
        """The store path hash prefix."""
        return self.base_name[: self.HashLen]

    @property
    def name(self) -> str:
        """The store path name after the hash separator."""
        if len(self.base_name) <= self.HashLen:
            return ""
        return self.base_name[self.HashLen + 1 :]

    @property
    def is_derivation(self) -> bool:
        """True if this path ends with .drv."""
        return self.name.endswith(".drv")


@dataclass(frozen=True)
class GcResult:
    """Result of a garbage collection operation."""

    paths: list[StorePath]
    bytes_freed: int


class LogEventExt(_LogEventProto):
    """Typed worker log event with Nix-log convenience accessors."""

    _ = _LogEventProto._betterproto  # noqa: SLF001
    _betterproto_meta = _LogEventProto._betterproto_meta  # noqa: SLF001

    def __init__(self, **kwargs: Any) -> None:
        if "args" in kwargs:
            args = kwargs.pop("args")
            nix_log = kwargs.get("nix_log") or NixLogEvent()
            kwargs["nix_log"] = NixLogEvent(
                action=kwargs.pop("action", nix_log.action),
                args_json=json.dumps(args),
                result_type=kwargs.pop("result_type", nix_log.result_type),
            )
        elif "action" in kwargs or "args_json" in kwargs or "result_type" in kwargs:
            rtype = kwargs.pop("result_type", None)
            if isinstance(rtype, int):
                rtype = ResultType(rtype)
            kwargs["nix_log"] = NixLogEvent(
                action=kwargs.pop("action", ""),
                args_json=kwargs.pop("args_json", ""),
                result_type=rtype,
            )
        super().__init__(**kwargs)

    @property
    def is_nix_log(self) -> bool:
        return self.nix_log is not None

    @property
    def is_request_finalized(self) -> bool:
        return self.request_finalized is not None

    @property
    def action(self) -> str | None:
        return None if self.nix_log is None else self.nix_log.action

    @property
    def result_type(self) -> ResultType | None:
        return None if self.nix_log is None else self.nix_log.result_type

    @property
    def args(self) -> list[Any]:
        """Parsed args from the JSON ``args_json`` field."""
        if self.nix_log is None:
            return []
        return json.loads(self.nix_log.args_json) if self.nix_log.args_json else []

    @property
    def message(self) -> str | None:
        """Raw message payload for log actions that carry text."""
        if self.action not in {"msg", "warn", "error"} or not self.args:
            return None
        message = self.args[-1]
        return message if isinstance(message, str) else None

    @property
    def message_without_ansi(self) -> str | None:
        """Message payload with ANSI color escapes removed."""
        message = self.message
        return None if message is None else _strip_ansi(message)

    def without_ansi(self) -> LogEventExt:
        """Return a new LogEventExt with ANSI escapes removed from string args."""
        cleaned = [_strip_ansi(a) if isinstance(a, str) else a for a in self.args]
        if self.nix_log is None:
            return self
        return LogEventExt(request_id=self.request_id, action=self.action, args=cleaned, result_type=self.result_type)

    @classmethod
    def from_proto(cls, proto_event: _LogEventProto) -> LogEventExt:
        """Construct a LogEventExt from a proto LogEvent message."""
        return cls(
            request_id=proto_event.request_id,
            nix_log=proto_event.nix_log,
            request_finalized=proto_event.request_finalized,
        )


# ══════════════════════════════════════════════════════════════════════════
# Public re-exports
# ══════════════════════════════════════════════════════════════════════════

LogEvent = LogEventExt

# ══════════════════════════════════════════════════════════════════════════
# NixType enum patch — add from_string classmethod
#
# NixType is generated by betterproto2 from common.proto and nanopynix
# doesn't own the class, so it can't gain a real classmethod via subclassing
# the way LogEventExt subclasses the generated LogEvent above -- proto enums
# are plain IntEnum-like classes, not designed for subclassing, and every
# call site that receives a NixType from the wire (worker responses) needs
# the *same* class, not a subclass only some sites would construct. Patching
# the classmethod onto the generated class directly, once at import time,
# is the only way for `NixType.from_string(...)` to work everywhere without
# threading a separate conversion function through every call site.
# ══════════════════════════════════════════════════════════════════════════

_STR_TO_NIX: dict[str, NixType] = {
    "thunk": NixType.THUNK,
    "int": NixType.INT,
    "float": NixType.FLOAT,
    "bool": NixType.BOOL,
    "string": NixType.STRING,
    "path": NixType.PATH,
    "null": NixType.NULL,
    "attrs": NixType.ATTRS,
    "list": NixType.LIST,
    "function": NixType.FUNCTION,
    "external": NixType.EXTERNAL,
    "unknown": NixType.UNSPECIFIED,
}


def _nix_type_from_string(_cls: type, value: str) -> NixType:
    return _STR_TO_NIX.get(value, NixType.UNSPECIFIED)


NixType.from_string = classmethod(_nix_type_from_string)  # type: ignore[attr-defined] -- proto-generated enum; attribute is dynamic

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
