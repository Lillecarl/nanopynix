"""Carry a worker error's structured detail across the worker RPC (boundary B).

A gRPC failure normally reduces to a status code plus one string. Two things
do not fit in that string, and both used to be lost:

* **Which class the worker raised.** The prose prefix on the status message
  recovers a Nix C++ class and nothing else, because that is the only
  vocabulary ``exceptions.from_response`` can read from it. Every other
  exception the worker raises reached the client as a plain ``NixError``.
  :class:`~nanopynix_proto.nix.common.ErrorIdentity` carries the class as a
  field instead.
* **The ``nix::ErrorInfo`` the bindings recovered on the C++ side**: source
  position, evaluation trace, suggestions. Without a channel for it, the rpc
  engine raises the right exception class with an empty
  :attr:`~nanopynix.NixError.info`, while the inproc engine raising the same
  failure has the full payload.

Process isolation is the only thing rpc has that inproc does not, and none of
that applies to an error's contents, so either asymmetry would be a defect
rather than a consequence.

gRPC's channel for exactly this is the ``grpc-status-details-bin`` trailer: a
serialized ``google.rpc.Status`` whose ``details`` are ``Any``-packed messages.
grpclib exposes it as ``GRPCError.details`` and encodes it with a
``StatusDetailsCodecBase``. Its built-in ``ProtoStatusDetailsCodec`` is not
usable here -- it drives the classic protobuf runtime's ``Any.Pack`` and symbol
database, while every message in this project is a betterproto2 pydantic
dataclass -- so :class:`NixStatusDetailsCodec` below does the same job through
betterproto2's message pool. The bytes on the wire are identical either way,
which is the point: a client in any language reads this with stock gRPC
tooling, needing nothing from nanopynix beyond ``common.proto``.

**Both ends must install the codec.** grpclib degrades silently otherwise --
the server omits the trailer and the client leaves ``details`` as ``None``, with
no error either side. See ``rpc/worker/_worker.py`` and
``rpc/client/_pool.py`` for the two installation points.

That silent degradation is why the worker still prefixes ``"TypeName: "`` onto
the status message. A peer without the codec recovers a Nix C++ class from the
prefix, which is what every client did before the identity existed. A peer with
the codec strips the duplicate.

The public :attr:`~nanopynix.NixError.info` stays a plain ``dict``, not
:class:`~nanopynix_proto.nix.common.NixErrorInfo`. On inproc there is no proto
anywhere in the path -- ``nix_error_info.hh`` builds the dict directly in C++ --
so making the message the public type would add a dict -> message -> dict
round-trip whose only purpose is to make the two engines *look* alike, and
would silently drop any field C++ emits that the schema has not caught up
with. The message is a wire encoding; the dict is the API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from grpclib.const import Status
from grpclib.encoding.base import StatusDetailsCodecBase
from nanopynix_proto.google.protobuf import Any as ProtoAny
from nanopynix_proto.google.rpc import Status as RpcStatus
from nanopynix_proto.nix.common import ErrorIdentity, ErrorTrace, LogLevel, NixErrorInfo, SourcePos
from pydantic import ValidationError

from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixError, ObjectMisuseError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from betterproto2 import Message

logger = logging.getLogger(__name__)

# Budget for the encoded `google.rpc.Status`, in bytes.
#
# This rides in an HTTP/2 *header*, base64-encoded (so the header itself is a
# further 4/3 of this), and an oversized header list is a connection-level
# protocol error rather than a truncated field. A deep evaluation trace easily
# runs past that, so the codec trims to fit rather than trusting it to be small.
MAX_DETAILS_BYTES = 6144


def error_info_from_dict(*, raw: str, info: dict[str, Any] | None) -> NixErrorInfo | None:
    """Build the wire message from an exception's ``raw``/``info``, or ``None``.

    ``None`` means "nothing to send", so the trailer is omitted entirely rather
    than carrying an empty message.

    Tolerant by construction. The input is whatever ``nix_error_info.hh``
    attached, and this runs on a path that is *already* reporting a failure --
    replacing the caller's Nix error with a :class:`ValidationError` raised by
    the error reporter would be strictly worse than losing the detail. Fields
    that do not fit the schema (an out-of-int32 ``status``, a malformed
    ``pos``) are dropped individually; only a wholly unusable payload degrades
    to ``None``.
    """
    if not raw and not info:
        return None
    try:
        return _build_error_info(raw=raw, info=info or {})
    except (ValidationError, TypeError, ValueError):
        logger.warning("could not encode Nix error detail; sending it unstructured", exc_info=True)
        try:
            return NixErrorInfo(raw=raw)
        except ValidationError:
            return None


def error_info_to_dict(message: NixErrorInfo) -> dict[str, Any]:
    """Render the wire message back into the shape ``nix_error_info.hh`` emits.

    Key-for-key identical to the C++ dict, so an rpc caller's
    :attr:`~nanopynix.NixError.info` compares equal to an inproc caller's for
    the same failure. ``raw`` is deliberately absent: it travels in the same
    message but is a sibling of ``info``, not a member of it.
    """
    return {
        "level": int(message.level),
        "msg": message.msg,
        "pos": _pos_to_dict(message.pos),
        "is_from_expr": message.is_from_expr,
        "status": message.status,
        "traces": [{"hint": trace.hint, "pos": _pos_to_dict(trace.pos)} for trace in message.traces],
        "truncated": message.truncated,
        "suggestions": list(message.suggestions),
    }


class NixStatusDetailsCodec(StatusDetailsCodecBase):
    """``google.rpc.Status`` codec for betterproto2 messages.

    Same wire format as grpclib's ``ProtoStatusDetailsCodec``, but packing and
    unpacking through betterproto2's message pool rather than the classic
    protobuf runtime's ``Any.Pack`` and symbol database, which cannot see
    betterproto2's message types.

    Stateless and picklable by design -- the worker runs in a forkserver child,
    so the codec instance is passed through ``multiprocessing`` process args.
    """

    def encode(self, status: Status, message: str | None, details: Sequence[Message]) -> bytes:
        proto = RpcStatus(
            code=status.value,
            message=message or "",
            details=[ProtoAny.pack(detail) for detail in details],
        )
        encoded = bytes(proto)
        if len(encoded) <= MAX_DETAILS_BYTES:
            return encoded
        return bytes(_trim_to_budget(proto))

    def decode(self, status: Status, message: str | None, data: bytes) -> Sequence[Any]:
        try:
            proto = RpcStatus.parse(data)
        except Exception:
            # A malformed trailer must not replace the caller's Nix error with
            # a decoding error. Losing the detail is the correct degradation.
            logger.warning("could not decode gRPC status details", exc_info=True)
            return []
        decoded: list[Any] = []
        for detail in proto.details:
            try:
                unpacked = detail.unpack()
            except TypeError:
                # A detail type this build does not know -- expected across
                # versions. The remaining details are still usable.
                logger.debug("skipping unknown status detail %r", detail.type_url)
                continue
            if unpacked is not None:
                decoded.append(unpacked)
        return decoded


NIX_STATUS_DETAILS_CODEC = NixStatusDetailsCodec()


def identity_for_exception(exc: BaseException) -> ErrorIdentity:
    """Name the exception's class, in whichever of the two vocabularies fits.

    ``__module__`` is the discriminator, exactly as in
    ``translate_nix_exception``: a class from ``nanopynix_bindings`` is a Nix
    C++ class, and its Python name *is* the C++ name that
    :func:`~nanopynix.exceptions.exception_for_nix_type` looks up. Anything
    else is a nanopynix or a Python class, and the client resolves it against
    its own allowlist.

    Never ``None``. Every exception has a class, and the class is the one
    thing the client can always use.
    """
    name = type(exc).__name__
    if type(exc).__module__.startswith("nanopynix_bindings"):
        return ErrorIdentity(nix_type=name)
    return ErrorIdentity(class_name=name)


def status_for_exception(exc: BaseException) -> Status:
    """Choose the gRPC status code for a worker failure.

    A client in another language reads the code before it reads anything else,
    and a blanket ``UNKNOWN`` tells it nothing. Two families have a code that
    means what gRPC says it means:

    * :class:`~nanopynix.ObjectMisuseError` -- the object's state is wrong for
      the call, which is ``FAILED_PRECONDITION`` in as many words.
    * the four argument-shaped builtins -- the request named an index, a
      handle or a type the worker cannot use, which is ``INVALID_ARGUMENT``.

    Everything else stays ``UNKNOWN``, including every :class:`NixError` and
    every Nix C++ error. A Nix failure is a failure of the requested *work*,
    not of the request, and gRPC has no code for that. ``DEADLINE_EXCEEDED``
    and ``UNIMPLEMENTED`` stay unused for the opposite reason: both mean
    something specific to gRPC middleware that a worker-internal
    ``TimeoutError`` or a ``nix::UnimplementedError`` is not.
    """
    if isinstance(exc, ObjectMisuseError):
        return Status.FAILED_PRECONDITION
    # `NixError` first: `MissingAttributeError` is also a `KeyError` and
    # `ListIndexError` is also an `IndexError`, and neither is a bad argument
    # to the RPC -- Nix answered, and the answer was "not there".
    if isinstance(exc, NixError):
        return Status.UNKNOWN
    if isinstance(exc, (IndexError, KeyError, TypeError, ValueError)):
        return Status.INVALID_ARGUMENT
    return Status.UNKNOWN


def details_for_exception(exc: BaseException) -> list[Message]:
    """Everything the status trailer carries for one worker exception.

    Always at least the :class:`ErrorIdentity`, which is what lets the client
    raise the class the worker raised. The :class:`NixErrorInfo` joins it when
    the exception has one, in two shapes that one lookup covers because the
    bindings and :class:`~nanopynix.NixError` agree on the names:

    * a raw nanobind binding exception, with ``raw``/``info`` attached by
      ``nix_error_info.hh``. This is the common case -- the worker
      deliberately does *not* run ``translate_nix_exception``, because the
      identity carries the Nix C++ class name and translating would replace
      that with the public one.
    * an already-translated :class:`~nanopynix.NixError` from worker-side
      helper code, where ``raw``/``info`` are declared attributes.

    Guarded by type rather than by ``hasattr``: this runs on every unhandled
    handler exception, including ones that have nothing to do with Nix and may
    happen to carry an unrelated ``info``.
    """
    raw: object = getattr(exc, "raw", None)
    info: object = getattr(exc, "info", None)
    encoded = error_info_from_dict(
        raw=raw if isinstance(raw, str) else "",
        info=cast("dict[str, Any]", info) if isinstance(info, dict) else None,
    )
    details: list[Message] = [identity_for_exception(exc)]
    if encoded is not None:
        details.append(encoded)
    return details


@dataclass(frozen=True, slots=True)
class ReceivedError:
    """What the status trailer of a failed RPC told the client.

    Every field has an "absent" value that is also its default, because a peer
    with no codec sends no trailer at all and that has to stay a loss of
    detail rather than a second failure.
    """

    nix_type: str = ""
    """The Nix C++ class the worker named, or ``""``."""

    class_name: str = ""
    """The nanopynix or Python class the worker named, or ``""``."""

    raw: str = ""
    """``showErrorInfo``'s rendering, for :attr:`~nanopynix.NixError.raw`."""

    info: dict[str, Any] | None = None
    """The ``nix::ErrorInfo`` dict, for :attr:`~nanopynix.NixError.info`."""


def unpack_error_details(details: Any) -> ReceivedError:
    """Read a received ``GRPCError.details`` back into its parts.

    Tolerant on purpose: ``details`` is ``None`` whenever the peer has no codec
    installed, and the contents are only as trustworthy as the peer. Anything
    unexpected degrades to "no detail", never to an exception -- this runs on
    the path that is already reporting a failure.
    """
    if not isinstance(details, list):
        return ReceivedError()
    identity = ErrorIdentity()
    raw = ""
    info: dict[str, Any] | None = None
    for detail in cast("list[Any]", details):
        if isinstance(detail, ErrorIdentity):
            identity = detail
        elif isinstance(detail, NixErrorInfo):
            raw, info = detail.raw, error_info_to_dict(detail)
    return ReceivedError(
        nix_type=identity.nix_type,
        class_name=identity.class_name,
        raw=raw,
        info=info,
    )


def _build_error_info(*, raw: str, info: dict[str, Any]) -> NixErrorInfo:
    level: object = info.get("level", 0)
    status: object = info.get("status", 0)
    traces: object = info.get("traces", [])
    suggestions: object = info.get("suggestions", [])
    return NixErrorInfo(
        raw=raw,
        # LogLevel keeps out-of-range values as `UNKNOWN(n)` rather than
        # rejecting them, so an unexpected nix::Verbosity survives the trip.
        level=LogLevel(level) if isinstance(level, int) else LogLevel(0),
        msg=_as_str(info.get("msg")),
        pos=_pos_from_dict(info.get("pos")),
        is_from_expr=bool(info.get("is_from_expr", False)),
        # int32, unlike `level` -- pydantic rejects anything wider, and a
        # nix::ExitCode that does not fit is not worth losing the rest over.
        status=status if isinstance(status, int) and -(2**31) <= status < 2**31 else 0,
        traces=[_trace_from_dict(trace) for trace in cast("list[Any]", traces)] if isinstance(traces, list) else [],
        truncated=bool(info.get("truncated", False)),
        suggestions=[_as_str(s) for s in cast("list[Any]", suggestions)] if isinstance(suggestions, list) else [],
    )


def _trace_from_dict(trace: object) -> ErrorTrace:
    if not isinstance(trace, dict):
        return ErrorTrace()
    entry = cast("dict[str, Any]", trace)
    return ErrorTrace(hint=_as_str(entry.get("hint")), pos=_pos_from_dict(entry.get("pos")))


def _pos_from_dict(pos: object) -> SourcePos | None:
    if not isinstance(pos, dict):
        return None
    entry = cast("dict[str, Any]", pos)
    line: object = entry.get("line", 0)
    column: object = entry.get("column", 0)
    return SourcePos(
        file=_as_str(entry.get("file")),
        line=line if isinstance(line, int) and 0 <= line < 2**32 else 0,
        column=column if isinstance(column, int) and 0 <= column < 2**32 else 0,
    )


def _pos_to_dict(pos: SourcePos | None) -> dict[str, Any] | None:
    if pos is None:
        return None
    return {"file": pos.file, "line": pos.line, "column": pos.column}


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _trim_to_budget(proto: RpcStatus) -> RpcStatus:
    """Shrink an oversized ``Status`` until it fits :data:`MAX_DETAILS_BYTES`.

    The :class:`ErrorIdentity` is never trimmed. It is a few dozen bytes, and
    it is the one detail that decides which exception the client raises;
    losing it on a deep trace would mean the errors with the most to say
    arrive as the least specific class.

    Of the rest, ``raw`` goes first: it is ``showErrorInfo``'s rendering of
    the very fields beside it, so it is the half a reader can reconstruct.
    Trace frames go next, from the tail, since the innermost frames are the
    ones worth keeping. Anything dropped sets ``truncated``, so a reader can
    tell "no trace" from "trace discarded" -- the same flag the C++ side sets
    when it hits its own frame cap.
    """
    unpacked = [detail.unpack() for detail in proto.details]
    kept = [detail for detail in unpacked if isinstance(detail, ErrorIdentity)]
    payloads = [detail for detail in unpacked if isinstance(detail, NixErrorInfo)]
    if len(kept) + len(payloads) != len(unpacked):
        # A detail this function does not know how to shrink is taking up the
        # budget. Keep the identity, drop the rest: an unshrinkable payload
        # must not cost the client the exception class as well.
        return _repack(proto, kept)

    for payload in payloads:
        if payload.raw:
            payload.raw = ""
            payload.truncated = True
    proto = _repack(proto, [*kept, *payloads])

    # Deepest trace first, so the budget is reclaimed from whichever detail is
    # actually responsible for the overflow.
    while len(bytes(proto)) > MAX_DETAILS_BYTES:
        deepest = max(payloads, key=lambda payload: len(payload.traces))
        if not deepest.traces:
            break
        deepest.traces.pop()
        deepest.truncated = True
        proto = _repack(proto, [*kept, *payloads])

    if len(bytes(proto)) > MAX_DETAILS_BYTES:
        # A single hint or message is itself enormous, past what the C++
        # per-string cap should allow. Send the identity and the status
        # message alone rather than risk a protocol error that loses the
        # error entirely.
        return _repack(proto, kept)
    return proto


def _repack(proto: RpcStatus, payloads: Sequence[Message]) -> RpcStatus:
    return RpcStatus(
        code=proto.code,
        message=proto.message,
        details=[ProtoAny.pack(payload) for payload in payloads],
    )
