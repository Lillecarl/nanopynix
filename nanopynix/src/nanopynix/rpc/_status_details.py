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

gRPC's channel for exactly this is the ``grpc-status-details-bin`` trailer,
which grpclib exposes as ``GRPCError.details`` and encodes with a
``StatusDetailsCodecBase``. The canonical codec is protobuf ``google.rpc.Status``,
which is unavailable here (``grpclib.client._googleapis_available()`` is false
without googleapis-common-protos), and would buy nothing: the payload is
already a plain JSON-shaped dict built by ``nix_error_info.hh``.

**Both ends must install the codec.** grpclib degrades silently otherwise --
the server omits the trailer and the client leaves ``details`` as ``None``, with
no error either side. See ``rpc/worker/_worker.py`` and
``rpc/client/_pool.py`` for the two installation points.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from grpclib.encoding.base import StatusDetailsCodecBase

if TYPE_CHECKING:
    from grpclib.const import Status

# Budget for the encoded payload, in bytes.
#
# This rides in an HTTP/2 *header*, base64-encoded, so it is not free the way a
# message body is: an oversized header list is a connection-level protocol
# error, not a truncated field. A deep evaluation trace easily runs past that,
# so `pack_error_details` trims to fit rather than trusting it to be small.
MAX_DETAILS_BYTES = 6144


class JsonStatusDetailsCodec(StatusDetailsCodecBase):
    """Encode ``GRPCError.details`` as JSON.

    Stateless and picklable by design -- the worker runs in a forkserver child,
    so the codec instance is passed through ``multiprocessing`` process args.
    """

    def encode(self, status: Status, message: str | None, details: Any) -> bytes:
        return json.dumps(details, separators=(",", ":")).encode("utf-8")

    def decode(self, status: Status, message: str | None, data: bytes) -> Any:
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # A malformed trailer must not replace the caller's Nix error with
            # a decoding error. Losing the detail is the correct degradation.
            return None


NIX_STATUS_DETAILS_CODEC = JsonStatusDetailsCodec()


def pack_error_details(*, raw: str, info: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the wire payload for an exception's ``raw``/``info``, or ``None``.

    Returns ``None`` when there is nothing to send, so the trailer is omitted
    entirely rather than carrying an empty object.

    Oversized payloads are trimmed to :data:`MAX_DETAILS_BYTES` by dropping
    trace frames from the tail -- the innermost frames are the ones a reader
    wants -- and then, if still too large, the rendered ``raw``. Whatever is
    dropped sets ``info["truncated"]``, so a reader can distinguish "no trace"
    from "trace discarded", the same flag the C++ side already sets when it
    caps trace count.
    """
    if not raw and not info:
        return None

    payload: dict[str, Any] = {"raw": raw, "info": info}
    if _encoded_size(payload) <= MAX_DETAILS_BYTES:
        return payload

    if info is not None and isinstance(info.get("traces"), list):
        traces = list(info["traces"])
        while traces and _encoded_size(payload) > MAX_DETAILS_BYTES:
            traces.pop()
            payload["info"] = {**info, "traces": traces, "truncated": True}

    if _encoded_size(payload) > MAX_DETAILS_BYTES:
        payload["raw"] = ""
        if isinstance(payload["info"], dict):
            payload["info"] = {**payload["info"], "truncated": True}

    # Still too large means a single hint or message is itself enormous, past
    # what the C++ per-string cap should allow. Send the type-bearing message
    # alone rather than risk a protocol error that loses the error entirely.
    if _encoded_size(payload) > MAX_DETAILS_BYTES:
        return None
    return payload


def details_for_exception(exc: BaseException) -> dict[str, Any] | None:
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
    return pack_error_details(
        raw=raw if isinstance(raw, str) else "",
        info=cast("dict[str, Any]", info) if isinstance(info, dict) else None,
    )


def unpack_error_details(details: Any) -> tuple[str, dict[str, Any] | None]:
    """Read a received ``GRPCError.details`` back into ``(raw, info)``.

    Tolerant on purpose: ``details`` is ``None`` whenever the peer has no codec
    installed, and the shape is only as trustworthy as the peer. Anything
    unexpected degrades to "no detail", never to an exception -- this runs on
    the path that is already reporting a failure.
    """
    if not isinstance(details, dict):
        return "", None
    payload = cast("dict[str, Any]", details)
    raw: object = payload.get("raw")
    info: object = payload.get("info")
    return (
        raw if isinstance(raw, str) else "",
        cast("dict[str, Any]", info) if isinstance(info, dict) else None,
    )


def _encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
