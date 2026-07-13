"""Models for all nanopynix data types crossing the C++/Python boundary.

Most types are re-exported from ``nanopynix_proto.nix.common`` — the
proto-generated messages are the canonical wire format.  A few types use
extension subclasses to add helper methods that the proto generated code
doesn't provide (``is_derivation``, ``message``, etc.).
"""

from __future__ import annotations

import json
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
    ForceValue as ForceValue,
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
    StorePath as _StorePath,
)
from nanopynix_proto.nix.common import (
    ValueHandle as ValueHandle,
)
from strip_ansi import strip_ansi as _strip_ansi  # type: ignore[reportMissingTypeStubs] -- strip_ansi has no PEP 561 stubs

# ══════════════════════════════════════════════════════════════════════════
# Helper: extract the StorePath basename
# ══════════════════════════════════════════════════════════════════════════


def _store_path_base_name(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


# ══════════════════════════════════════════════════════════════════════════
# Extension subclasses — add helper methods on top of proto-generated types.
# ══════════════════════════════════════════════════════════════════════════


class StorePathExt(_StorePath):
    """Extension of proto StorePath with ``is_derivation`` and string constructor."""

    _ = _StorePath._betterproto
    _betterproto_meta = _StorePath._betterproto_meta

    HashLen = 32
    MaxPathLen = 211

    def __new__(cls, value: str | _StorePath | None = None, **data: Any) -> StorePathExt:
        if isinstance(value, cls) and not data:
            return value
        return super().__new__(cls)

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
    def to_string(self) -> str:
        """The store path basename."""
        return self.base_name

    @property
    def is_derivation(self) -> bool:
        """True if this path ends with .drv."""
        return self.name.endswith(".drv")

    def __init__(self, value: str | _StorePath | None = None, **data: Any) -> None:
        if isinstance(value, _StorePath):
            if data:
                raise TypeError("StorePath accepts either a proto StorePath or explicit fields, not both")
            if value is self:
                return
            data = {"base_name": value.base_name}
            super().__init__(**data)
            return
        if value is not None:
            if data:
                raise TypeError("StorePath accepts either a path string or explicit fields, not both")
            data = {"base_name": _store_path_base_name(value)}
        super().__init__(**data)


class LogEventExt(_LogEventProto):
    """Extension of proto LogEvent with ``message``, ``message_without_ansi``, and ``args``."""

    _ = _LogEventProto._betterproto
    _betterproto_meta = _LogEventProto._betterproto_meta

    def __init__(self, **kwargs: Any) -> None:
        if "args" in kwargs:
            kwargs["args_json"] = json.dumps(kwargs.pop("args"))
        if "result_type" in kwargs:
            rtype = kwargs["result_type"]
            if isinstance(rtype, int):
                kwargs["result_type"] = ResultType(rtype)
        super().__init__(**kwargs)

    @property
    def args(self) -> list[Any]:
        """Parsed args from the JSON ``args_json`` field."""
        return json.loads(self.args_json) if self.args_json else []

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
        return LogEventExt(
            request_id=self.request_id,
            action=self.action,
            args_json=json.dumps(cleaned),
            result_type=self.result_type,
        )

    @classmethod
    def from_proto(cls, proto_event: _LogEventProto) -> LogEventExt:
        """Construct a LogEventExt from a proto LogEvent message."""
        return cls(
            request_id=proto_event.request_id,
            action=proto_event.action,
            args_json=proto_event.args_json,
            result_type=proto_event.result_type,
        )


# ══════════════════════════════════════════════════════════════════════════
# Public re-exports
# ══════════════════════════════════════════════════════════════════════════

LogEvent = LogEventExt
StorePath = StorePathExt

# ══════════════════════════════════════════════════════════════════════════
# NixType enum patch — add from_string classmethod
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

type CallArgWire = CallArg
type DeepValueWire = DeepValue
type RemoteValueRef = RemoteCallArg
