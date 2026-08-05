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
    CallArg as CallArg,
    CallArgAttrs as CallArgAttrs,
    CallArgList as CallArgList,
    DeepAttrs as DeepAttrs,
    DeepList as DeepList,
    DeepValue as DeepValue,
    Derivation as Derivation,
    DerivationOutput as DerivationOutput,
    DerivationOutputs as DerivationOutputs,
    FlakeRef as FlakeRef,
    Input as Input,
    LockedFlake as LockedFlake,
    LockedInput as LockedInput,
    LogEvent as _LogEventProto,
    MissingInfo as MissingInfo,
    NixLogEvent as NixLogEvent,
    NixType as NixType,
    NullValue as NullValue,
    PathInfo as PathInfo,
    PrimOpSpec as PrimOpSpec,
    RemoteCallArg as RemoteCallArg,
    ResultType as ResultType,
    ScalarValue as ScalarValue,
    ValueHandle as ValueHandle,
)
from nanopynix_proto.nix.store import (
    GcRoot as GcRoot,
)

from nanopynix._ansi import strip_ansi as _strip_ansi


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


class DerivedPath(str):
    """A derived-path string in Nix's ``^`` notation, with its parts named.

    Nix's ``DerivedPath`` is a sum type and this string is its serialization:
    ``<drv>^out``, ``<drv>^*``, or a bare store path. Every nanopynix entry
    point that builds or queries something accepts this spelling -- ``build``,
    ``build_paths_with_results``, ``query_missing`` -- so this is a name for
    syntax the API already takes, not a new concept.

    It splits and nothing more. In particular it does not decide what a string
    with *no* selector means, which is why :attr:`outputs` reports ``None``
    rather than ``[]`` for that case::

        DerivedPath("/nix/store/h-x.drv").outputs      # None -- nothing selected
        DerivedPath("/nix/store/h-x.drv^*").outputs    # ["*"] -- every output
        DerivedPath("/nix/store/h-x.drv^dev,out").outputs  # ["dev", "out"]

    The distinction matters because a bare ``.drv`` means different things at
    different layers. To Nix it is ``DerivedPath::Opaque`` -- "fetch this
    path" -- so ``nix build <drv>`` builds nothing. The async stores of both
    engines read it as "build every output" instead, which is what
    :meth:`for_build` below does for them. ``None`` here says "the string did
    not say" rather than picking one of those. It is also what keeps this
    readable next to :attr:`BuildResult.outputs`, where ``[]`` is a real
    answer meaning ``DerivedPath::Opaque``.
    """

    __slots__ = ()

    SEPARATOR = "^"

    def __new__(cls, value: str) -> DerivedPath:
        if isinstance(value, cls):
            return value
        return super().__new__(cls, value)

    @property
    def drv_path(self) -> StorePath:
        """The store path before the selector, or the whole string if there is none.

        Splits on the *last* ``^``, as Nix's own ``parseWith``
        (``derived-path.cc``) does. With ``dynamic-derivations`` that leaves a
        head which is itself derived (``a.drv^out^bin`` → ``a.drv^out``) and so
        is not a store path; Nix has the same shape there, and reporting the
        head verbatim is the only answer that does not lose it.
        """
        return StorePath(self.rpartition(self.SEPARATOR)[0] or self)

    @property
    def outputs(self) -> list[str] | None:
        """The selected outputs, or ``None`` when the string carries no selector.

        ``["*"]`` for ``^*``. Names are returned in the order written, not
        sorted -- Nix canonicalises them, and reordering here would hide
        whether a round trip actually reached ``DerivedPath::parse``.
        """
        head, separator, tail = self.rpartition(self.SEPARATOR)
        if not separator or not head:
            return None
        return tail.split(",") if tail else []

    def for_build(self) -> DerivedPath:
        """Append ``^*`` when this is a bare ``.drv``, and return it unchanged otherwise.

        A bare ``.drv`` is ``DerivedPath::Opaque`` to Nix, which asks the store
        to make the derivation *file* present and builds none of its outputs.
        ``nix build <drv>`` does exactly that, and reports success having done
        nothing -- which reads as "already up to date" to a caller who wanted
        the outputs. ``Store.query_missing`` and
        ``Store.build_paths_with_results`` of both engines therefore call this
        first, so that a bare ``.drv`` selects every output.

        The conversion is here, and not in the bindings, so that
        ``nanopynix_bindings`` keeps Nix's own semantics for a function that
        maps a Nix one. Everything else passes through: a bare
        non-derivation path stays opaque, because a fetch is what that
        genuinely means, and a string that already carries ``^`` said what it
        wanted.

        No store configuration is needed for this. Nix resolves a relative
        store path before it looks for the separator, so ``<drv>^*`` is
        correct whether or not the path is absolute.
        """
        if self.SEPARATOR in self or not self.endswith(".drv"):
            return self
        return DerivedPath(f"{self}{self.SEPARATOR}*")


@dataclass(frozen=True)
class GcResult:
    """Result of a garbage collection operation."""

    paths: list[StorePath]
    bytes_freed: int


class LogEventExt(_LogEventProto):
    """Typed worker log event with Nix-log convenience accessors."""

    _ = _LogEventProto._betterproto  # noqa: SLF001 -- forces the parent's classproperty to compute and cache its metadata first
    _betterproto_meta = _LogEventProto._betterproto_meta  # noqa: SLF001 -- copies the cached metadata so this handwritten subclass never recomputes it via get_type_hints in models.py's namespace

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
    def is_events_dropped(self) -> bool:
        return self.events_dropped is not None

    @property
    def dropped_count(self) -> int:
        """How many events the producer discarded before this point.

        Zero for every other kind of event, so a consumer can add this without
        a kind test.
        """
        return 0 if self.events_dropped is None else self.events_dropped.count

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
        """:attr:`message` as Nix would print it to a terminal that has no colour.

        This calls :func:`nanopynix.strip_ansi`, which removes every escape
        sequence and not only a colour. It also expands each tab and drops a
        carriage return and a bell, so read that function before you compare
        the result against an exact string.
        """
        message = self.message
        return None if message is None else _strip_ansi(message)

    def without_ansi(self) -> LogEventExt:
        """Return a new LogEventExt, with each string argument filtered.

        See :attr:`message_without_ansi` for what the filter does beside the
        removal of a colour. A build log carries a carriage return often, and
        this drops each one.
        """
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
            events_dropped=proto_event.events_dropped,
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
