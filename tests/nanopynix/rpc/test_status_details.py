"""Unit tests for the ``grpc-status-details-bin`` payload (boundary B).

These exercise the parts of :mod:`nanopynix.rpc._status_details` that a live
RPC round-trip cannot reach. Two categories:

* **The byte budget.** Real Nix errors carry at most a couple of trace frames,
  so the budget never binds and the trimming path never runs in the matrix.
  It is not decorative -- the payload rides an HTTP/2 header, where going over
  is a connection-level protocol error rather than a truncated field.
* **The dict <-> message conversion.** ``nix_error_info.hh`` builds a plain
  dict in C++, and the schema constrains what can be encoded (``status`` is an
  int32, ``pos`` is a message). A payload that does not fit must degrade to
  less detail, never to a :class:`ValidationError` raised *by the thing
  reporting an error*.
"""

from __future__ import annotations

import pickle
from typing import Any

import pytest
from grpclib.const import Status
from nanopynix_proto.google.protobuf import Any as ProtoAny
from nanopynix_proto.google.rpc import Status as RpcStatus
from nanopynix_proto.nix.common import LogLevel, NixErrorInfo

from nanopynix.exceptions import EvalError
from nanopynix.rpc._status_details import (
    MAX_DETAILS_BYTES,
    NixStatusDetailsCodec,
    details_for_exception,
    error_info_from_dict,
    error_info_to_dict,
    unpack_error_details,
)


def _info(*, traces: int = 0, msg: str = "boom", **overrides: Any) -> dict[str, Any]:
    """The shape ``nix_error_info.hh`` attaches, key for key."""
    return {
        "level": 0,
        "msg": msg,
        "pos": {"file": "«string»", "line": 1, "column": 1},
        "is_from_expr": True,
        "status": 1,
        "traces": [
            {"hint": f"while evaluating frame {i} " + "x" * 200, "pos": None} for i in range(traces)
        ],
        "truncated": False,
        "suggestions": [],
        **overrides,
    }


def _encode(raw: str = "error: boom", info: dict[str, Any] | None = None) -> bytes:
    encoded = error_info_from_dict(raw=raw, info=info)
    details = [] if encoded is None else [encoded]
    return NixStatusDetailsCodec().encode(Status.UNKNOWN, "EvalError: boom", details)


def _decode(data: bytes) -> tuple[str, dict[str, Any] | None]:
    details = NixStatusDetailsCodec().decode(Status.UNKNOWN, "EvalError: boom", data)
    return unpack_error_details(list(details))


# ── dict <-> message conversion ───────────────────────────────────────


def test_conversion_is_lossless_for_a_real_error_info() -> None:
    """The rpc engine's `info` must compare equal to the inproc engine's.

    This is the same claim ``tests/temp/test_error_matrix.py`` makes end to
    end; asserted here on the encoding alone so a regression points at the
    codec rather than at the whole RPC stack.
    """
    info = _info(traces=3)
    encoded = error_info_from_dict(raw="error: boom", info=info)

    assert encoded is not None
    assert error_info_to_dict(encoded) == info


def test_conversion_returns_none_when_there_is_nothing_to_send() -> None:
    assert error_info_from_dict(raw="", info=None) is None
    assert error_info_from_dict(raw="", info={}) is None


def test_raw_is_carried_beside_info_not_inside_it() -> None:
    encoded = error_info_from_dict(raw="error: boom", info=_info())

    assert encoded is not None
    assert encoded.raw == "error: boom"
    assert "raw" not in error_info_to_dict(encoded), "raw is a sibling of info, not a member"


def test_out_of_int32_status_degrades_to_zero_rather_than_raising() -> None:
    """`status` is an int32 in the schema; pydantic rejects anything wider.

    Losing one field beats replacing the caller's Nix error with a
    ValidationError raised by the code reporting it.
    """
    encoded = error_info_from_dict(raw="", info=_info(status=2**40))

    assert encoded is not None
    assert encoded.status == 0
    assert encoded.msg == "boom", "the rest of the payload survives"


def test_unknown_verbosity_survives_rather_than_being_clamped() -> None:
    """Unlike `status`, betterproto2 enums keep out-of-range values."""
    encoded = error_info_from_dict(raw="", info=_info(level=99))

    assert encoded is not None
    assert int(encoded.level) == 99
    assert error_info_to_dict(encoded)["level"] == 99


@pytest.mark.parametrize(
    "info",
    [
        {"pos": None},
        {"pos": "not a dict"},
        {"msg": None},
        {"traces": "not a list"},
        {"traces": [None, 42, {"hint": "ok"}]},
        {"suggestions": None},
        {"level": "not an int"},
        {"is_from_expr": "truthy"},
    ],
    ids=lambda info: next(iter(info)) + "=" + str(next(iter(info.values())))[:16],
)
def test_malformed_info_fields_degrade_individually(info: dict[str, Any]) -> None:
    # Merged rather than passed through `_info(**info)`: its `traces` kwarg is
    # a frame *count*, and these cases override the field itself.
    encoded = error_info_from_dict(raw="error: boom", info={**_info(), **info})

    assert encoded is not None
    assert encoded.raw == "error: boom"
    # Whatever was malformed, the message must still round-trip through the wire.
    assert NixErrorInfo.parse(bytes(encoded)) == encoded


def test_pos_absent_round_trips_as_none_not_as_an_empty_message() -> None:
    """A `pos` of None means "no usable position", which is not the same as a
    position at line 0 of an empty file -- the matrix compares these directly."""
    encoded = error_info_from_dict(raw="", info=_info(pos=None))

    assert encoded is not None
    assert error_info_to_dict(NixErrorInfo.parse(bytes(encoded)))["pos"] is None


# ── the byte budget ───────────────────────────────────────────────────


def test_a_small_payload_is_not_trimmed() -> None:
    raw, info = _decode(_encode(info=_info(traces=1)))

    assert raw == "error: boom"
    assert info == _info(traces=1)


def test_raw_is_dropped_before_any_trace_frame() -> None:
    """`raw` is the rendered form of what `info` already holds structurally.

    Spending the budget on it would mean discarding real structure to keep a
    string the reader can reconstruct, so it goes first.
    """
    raw, info = _decode(_encode(raw="e" * MAX_DETAILS_BYTES, info=_info(traces=4)))

    assert raw == ""
    assert info is not None
    assert len(info["traces"]) == 4, "traces must survive a large `raw`"
    assert info["truncated"] is True


def test_traces_are_trimmed_from_the_tail_when_info_alone_is_oversize() -> None:
    original = _info(traces=200)
    encoded = _encode(info=original)

    assert len(encoded) <= MAX_DETAILS_BYTES
    _, info = _decode(encoded)
    assert info is not None
    kept = info["traces"]
    assert 0 < len(kept) < 200
    # Head kept, tail dropped: the innermost frames are the useful ones.
    assert kept == original["traces"][: len(kept)]
    assert info["truncated"] is True


def test_an_untrimmable_payload_gives_up_rather_than_overflow_the_header() -> None:
    """A single enormous `msg` is past what the C++ per-string cap allows.

    Dropping the trailer loses the detail; overflowing the header list loses
    the whole response to a protocol error. Prefer the former.
    """
    encoded = _encode(raw="", info=_info(msg="m" * (MAX_DETAILS_BYTES * 2)))

    assert len(encoded) <= MAX_DETAILS_BYTES
    assert _decode(encoded) == ("", None)
    # The status message itself -- which carries the exception type -- survives.
    assert RpcStatus.parse(encoded).message == "EvalError: boom"


# ── details_for_exception ─────────────────────────────────────────────


def test_details_for_exception_reads_a_translated_nix_error() -> None:
    exc = EvalError("EvalError", "boom", raw="error: boom", info=_info(traces=1))
    details = details_for_exception(exc)

    assert details is not None
    assert details[0].raw == "error: boom"
    assert details[0].msg == "boom"


def test_details_for_exception_reads_binding_style_attributes() -> None:
    """The worker's common case: a raw nanobind exception, not a NixError."""

    class _BoundError(RuntimeError):
        pass

    exc = _BoundError("boom")
    exc.raw = "error: boom"  # type: ignore[attr-defined] -- mimics nix_error_info.hh
    exc.info = _info()  # type: ignore[attr-defined] -- mimics nix_error_info.hh

    details = details_for_exception(exc)
    assert details is not None
    assert details[0].is_from_expr is True


@pytest.mark.parametrize(
    ("raw_attr", "info_attr"),
    [(None, None), (object(), object()), ("", None), (b"bytes", ["list"])],
)
def test_details_for_exception_ignores_wrong_typed_attributes(raw_attr: object, info_attr: object) -> None:
    """Guarded by type, not ``hasattr``: this runs on *every* unhandled handler
    exception, including non-Nix ones that may carry an unrelated ``info``."""
    exc = RuntimeError("boom")
    if raw_attr is not None:
        exc.raw = raw_attr  # type: ignore[attr-defined] -- deliberately wrong type
    if info_attr is not None:
        exc.info = info_attr  # type: ignore[attr-defined] -- deliberately wrong type

    assert details_for_exception(exc) is None


# ── decode robustness ─────────────────────────────────────────────────


@pytest.mark.parametrize("details", [None, [], "string", 42, {}, [None, 42], ["not a message"]])
def test_unpack_degrades_to_no_detail_rather_than_raising(details: object) -> None:
    """Degrading is mandatory here: the path is already reporting a failure, so
    a malformed trailer must not replace the caller's Nix error with its own."""
    assert unpack_error_details(details) == ("", None)


@pytest.mark.parametrize("data", [b"\xff\xff\xff\xff garbage", b"\x08", b"\x1a\xff"])
def test_codec_decode_returns_no_details_on_malformed_bytes(data: bytes) -> None:
    assert NixStatusDetailsCodec().decode(Status.UNKNOWN, "boom", data) == []


def test_codec_decode_skips_detail_types_this_build_does_not_know() -> None:
    """A peer may send details from a newer schema; the known ones still work."""
    known = error_info_from_dict(raw="error: boom", info=_info())
    assert known is not None
    proto = RpcStatus(
        code=2,
        message="EvalError: boom",
        details=[
            ProtoAny(type_url="type.googleapis.com/nix.future.SomethingNew", value=b"\x08\x01"),
            ProtoAny.pack(known),
        ],
    )

    raw, info = _decode(bytes(proto))
    assert raw == "error: boom"
    assert info is not None


def test_codec_emits_a_standard_google_rpc_status() -> None:
    """The whole reason for the proto encoding: any language's gRPC tooling
    reads this trailer, not just ours."""
    parsed = RpcStatus.parse(_encode(info=_info()))

    assert parsed.code == Status.UNKNOWN.value
    assert parsed.message == "EvalError: boom"
    assert parsed.details[0].type_url == "type.googleapis.com/nix.common.NixErrorInfo"


def test_codec_is_picklable_for_the_forkserver_worker() -> None:
    """The worker runs in a forkserver child, so the instance travels through
    ``multiprocessing`` process args."""
    codec = NixStatusDetailsCodec()
    revived = pickle.loads(pickle.dumps(codec))  # noqa: S301 -- our own bytes, produced one line above
    assert isinstance(revived, NixStatusDetailsCodec)


def test_log_level_names_match_nix_verbosity() -> None:
    """`level` is encoded as the existing LogLevel enum rather than a bare int,
    which is only correct because nix::Verbosity has the same ordering."""
    assert (int(LogLevel.ERROR), int(LogLevel.VOMIT)) == (0, 7)
