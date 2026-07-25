"""Carry a Nix error's structured detail across the worker RPC (boundary B).

A gRPC failure normally reduces to a status code plus one string. That is
enough to recover *which* exception type to raise -- the worker prefixes the
class name onto the message -- but not the ``nix::ErrorInfo`` the bindings
recovered on the C++ side: source position, evaluation trace, suggestions.
Without a channel for it, the rpc engine raises the right exception class with
an empty :attr:`~nanopynix.NixError.info`, while the inproc engine raising the
same failure has the full payload. Process isolation is the only thing rpc has
that inproc does not, and none of that applies to an error's contents, so the
asymmetry would be a defect rather than a consequence.

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
from typing import TYPE_CHECKING, Any, cast

from grpclib.encoding.base import StatusDetailsCodecBase
from nanopynix_proto.google.protobuf import Any as ProtoAny
from nanopynix_proto.google.rpc import Status as RpcStatus
from nanopynix_proto.nix.common import ErrorTrace, LogLevel, NixErrorInfo, SourcePos
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from betterproto2 import Message
    from grpclib.const import Status

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


def details_for_exception(exc: BaseException) -> list[NixErrorInfo] | None:
    """Extract an exception's structured Nix detail, if it has any.

    Worker handlers see errors in two shapes, and one lookup covers both
    because the bindings and :class:`~nanopynix.NixError` agree on the names:

    * a raw nanobind binding exception, with ``raw``/``info`` attached by
      ``nix_error_info.hh``. This is the common case -- the worker
      deliberately does *not* run ``translate_nix_exception``, because the
      wire protocol identifies the error by its Nix C++ class name and
      translating would replace that with the public one.
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
    return None if encoded is None else [encoded]


def unpack_error_details(details: Any) -> tuple[str, dict[str, Any] | None]:
    """Read a received ``GRPCError.details`` back into ``(raw, info)``.

    Tolerant on purpose: ``details`` is ``None`` whenever the peer has no codec
    installed, and the contents are only as trustworthy as the peer. Anything
    unexpected degrades to "no detail", never to an exception -- this runs on
    the path that is already reporting a failure.
    """
    if not isinstance(details, list):
        return "", None
    for detail in cast("list[Any]", details):
        if isinstance(detail, NixErrorInfo):
            return detail.raw, error_info_to_dict(detail)
    return "", None


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

    ``raw`` goes first: it is ``showErrorInfo``'s rendering of the very fields
    beside it, so it is the half a reader can reconstruct. Trace frames go
    next, from the tail, since the innermost frames are the ones worth
    keeping. Anything dropped sets ``truncated``, so a reader can tell "no
    trace" from "trace discarded" -- the same flag the C++ side sets when it
    hits its own frame cap.
    """
    unpacked = [detail.unpack() for detail in proto.details]
    payloads = [detail for detail in unpacked if isinstance(detail, NixErrorInfo)]
    if len(payloads) != len(unpacked):
        # Something untrimmable is taking up the budget; there is nothing
        # smarter to do than send the status with no details at all.
        return RpcStatus(code=proto.code, message=proto.message)

    for payload in payloads:
        if payload.raw:
            payload.raw = ""
            payload.truncated = True
    proto = _repack(proto, payloads)

    # Deepest trace first, so the budget is reclaimed from whichever detail is
    # actually responsible for the overflow.
    while len(bytes(proto)) > MAX_DETAILS_BYTES:
        deepest = max(payloads, key=lambda payload: len(payload.traces))
        if not deepest.traces:
            break
        deepest.traces.pop()
        deepest.truncated = True
        proto = _repack(proto, payloads)

    if len(bytes(proto)) > MAX_DETAILS_BYTES:
        # A single hint or message is itself enormous, past what the C++
        # per-string cap should allow. Send the type-bearing status message
        # alone rather than risk a protocol error that loses the error entirely.
        return RpcStatus(code=proto.code, message=proto.message)
    return proto


def _repack(proto: RpcStatus, payloads: list[NixErrorInfo]) -> RpcStatus:
    return RpcStatus(
        code=proto.code,
        message=proto.message,
        details=[ProtoAny.pack(payload) for payload in payloads],
    )
