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
* **The error identity.** ``ErrorIdentity`` names the class the worker raised,
  and it is what lets the client raise that class rather than guess from
  prose. It rides the same trailer, and it is the one detail the byte budget
  must never reclaim.
"""

from __future__ import annotations

import pickle
from typing import Any

import pytest
from grpclib.const import Status
from nanopynix_bindings import errors as nanopynix_errors
from nanopynix_proto.google.protobuf import Any as ProtoAny
from nanopynix_proto.google.rpc import Status as RpcStatus
from nanopynix_proto.nix.common import ErrorIdentity, LogLevel, NixErrorInfo, SourcePos

from nanopynix.exceptions import (
    EvalError,
    ListIndexError,
    MissingAttributeError,
    SettingNotLiveError,
    UnresolvedValueError,
)
from nanopynix.rpc._status_details import (
    MAX_DETAILS_BYTES,
    NixStatusDetailsCodec,
    ReceivedError,
    details_for_exception,
    error_info_from_dict,
    error_info_to_dict,
    identity_for_exception,
    status_for_exception,
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
        "traces": [{"hint": f"while evaluating frame {i} " + "x" * 200, "pos": None} for i in range(traces)],
        "truncated": False,
        "suggestions": [],
        **overrides,
    }


def _encode(raw: str = "error: boom", info: dict[str, Any] | None = None) -> bytes:
    """Encode the pair a real worker sends: the identity, then the Nix detail."""
    encoded = error_info_from_dict(raw=raw, info=info)
    details: list[Any] = [ErrorIdentity(nix_type="EvalError")]
    if encoded is not None:
        details.append(encoded)
    return NixStatusDetailsCodec().encode(Status.UNKNOWN, "EvalError: boom", details)


def _decode(data: bytes) -> ReceivedError:
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
    received = _decode(_encode(info=_info(traces=1)))

    assert received.raw == "error: boom"
    assert received.info == _info(traces=1)


def test_raw_is_dropped_before_any_trace_frame() -> None:
    """`raw` is the rendered form of what `info` already holds structurally.

    Spending the budget on it would mean discarding real structure to keep a
    string the reader can reconstruct, so it goes first.
    """
    received = _decode(_encode(raw="e" * MAX_DETAILS_BYTES, info=_info(traces=4)))

    assert received.raw == ""
    assert received.info is not None
    assert len(received.info["traces"]) == 4, "traces must survive a large `raw`"
    assert received.info["truncated"] is True


def test_traces_are_trimmed_from_the_tail_when_info_alone_is_oversize() -> None:
    original = _info(traces=200)
    encoded = _encode(info=original)

    assert len(encoded) <= MAX_DETAILS_BYTES
    info = _decode(encoded).info
    assert info is not None
    kept = info["traces"]
    assert 0 < len(kept) < 200
    # Head kept, tail dropped: the innermost frames are the useful ones.
    assert kept == original["traces"][: len(kept)]
    assert info["truncated"] is True


def test_a_thousand_frame_trace_still_fits_and_still_says_it_was_cut() -> None:
    """The trimmer at five times the depth the other tests use (#19).

    A trace this deep is what an infinite recursion produces, so it is the
    realistic worst case rather than an invented one. Three things must hold
    at any depth, and each has its own way of going wrong:

    * the payload fits, or gRPC rejects the whole response;
    * ``truncated`` is set, or the caller reads a short trace as the complete
      one;
    * the frames kept are the **head**, because the innermost frames are the
      ones that say where the recursion turned.

    It also guards the cost, and that is what it was written for. The trimmer
    dropped one frame per pass and re-encoded the whole ``Status`` each time,
    which is quadratic: this test took 8.3s. A binary search over the cap
    gives the same answer in 0.08s.
    """
    original = _info(traces=1000)
    encoded = _encode(info=original)

    assert len(encoded) <= MAX_DETAILS_BYTES
    info = _decode(encoded).info
    assert info is not None
    kept = info["traces"]
    assert 0 < len(kept) < 1000
    assert kept == original["traces"][: len(kept)]
    assert info["truncated"] is True
    assert _decode(encoded).nix_type == "EvalError"

    # The cap is the largest that fits, not merely one that fits. A binary
    # search that stopped a frame early would satisfy every assertion above
    # while quietly throwing away trace the budget had room for.
    exact = _decode(_encode(info=_info(traces=len(kept)))).info
    assert exact is not None
    assert len(exact["traces"]) == len(kept), "the kept count does not survive being sent on its own"
    assert exact["truncated"] is False, "the kept count was itself trimmed, so the cap is too high"

    one_more = _decode(_encode(info=_info(traces=len(kept) + 1))).info
    assert one_more is not None
    assert len(one_more["traces"]) == len(kept), "one more frame fits, so the cap is not maximal"


def test_the_identity_survives_a_trim_that_empties_the_nix_detail() -> None:
    """The budget must never reclaim the class.

    A deep trace is exactly when the trimmer runs, so an identity that the
    trimmer could drop would make the errors with the most to say arrive as
    the least specific class.
    """
    received = _decode(_encode(raw="e" * MAX_DETAILS_BYTES, info=_info(traces=200)))

    assert received.nix_type == "EvalError"


def test_an_untrimmable_payload_keeps_the_identity_and_drops_the_rest() -> None:
    """A single enormous `msg` is past what the C++ per-string cap allows.

    Dropping the Nix detail loses the position and the trace; overflowing the
    header list loses the whole response to a protocol error. Prefer the
    former -- but the identity is a few dozen bytes and decides which class
    the caller catches, so it stays.
    """
    encoded = _encode(raw="", info=_info(msg="m" * (MAX_DETAILS_BYTES * 2)))

    assert len(encoded) <= MAX_DETAILS_BYTES
    assert _decode(encoded) == ReceivedError(nix_type="EvalError")
    # The status message itself -- which also carries the exception type, for
    # a peer that cannot read the identity -- survives.
    assert RpcStatus.parse(encoded).message == "EvalError: boom"


def test_an_unshrinkable_detail_costs_the_nix_info_and_not_the_identity() -> None:
    """A detail this build cannot trim must not take the class down with it.

    The trimmer knows how to shrink a ``NixErrorInfo`` and nothing else. A
    newer peer, or a future message, therefore reaches the give-up path -- and
    the give-up path used to send no details at all.
    """
    identity = ErrorIdentity(nix_type="EvalError")
    # Any registered message the trimmer has no rule for. `SourcePos` is one
    # the schema already has, so this needs no fixture message of its own.
    unshrinkable = SourcePos(file="f" * (MAX_DETAILS_BYTES * 2))

    encoded = NixStatusDetailsCodec().encode(Status.UNKNOWN, "EvalError: boom", [identity, unshrinkable])

    assert len(encoded) <= MAX_DETAILS_BYTES
    assert [detail.unpack() for detail in RpcStatus.parse(encoded).details] == [identity]


# ── details_for_exception ─────────────────────────────────────────────


def _nix_info_in(details: list[Any]) -> NixErrorInfo:
    """The one ``NixErrorInfo`` among the details, by type rather than by index.

    The identity comes first in production, so an index would only record the
    current ordering.
    """
    found = [detail for detail in details if isinstance(detail, NixErrorInfo)]
    assert len(found) == 1, f"expected exactly one NixErrorInfo, got {details}"
    return found[0]


def test_details_for_exception_reads_a_translated_nix_error() -> None:
    exc = EvalError("EvalError", "boom", raw="error: boom", info=_info(traces=1))
    detail = _nix_info_in(details_for_exception(exc))

    assert detail.raw == "error: boom"
    assert detail.msg == "boom"


def test_details_for_exception_reads_binding_style_attributes() -> None:
    """The worker's common case: a raw nanobind exception, not a NixError."""

    class _BoundError(RuntimeError):
        pass

    exc = _BoundError("boom")
    exc.raw = "error: boom"  # type: ignore[attr-defined] -- mimics nix_error_info.hh
    exc.info = _info()  # type: ignore[attr-defined] -- mimics nix_error_info.hh

    assert _nix_info_in(details_for_exception(exc)).is_from_expr is True


@pytest.mark.parametrize(
    ("raw_attr", "info_attr"),
    [(None, None), (object(), object()), ("", None), (b"bytes", ["list"])],
)
def test_details_for_exception_ignores_wrong_typed_attributes(raw_attr: object, info_attr: object) -> None:
    """Guarded by type, not ``hasattr``: this runs on *every* unhandled handler
    exception, including non-Nix ones that may carry an unrelated ``info``.

    The identity still goes out. It costs a few dozen bytes and it is the only
    thing that tells the client this was a ``RuntimeError``.
    """
    exc = RuntimeError("boom")
    if raw_attr is not None:
        exc.raw = raw_attr  # type: ignore[attr-defined] -- deliberately wrong type
    if info_attr is not None:
        exc.info = info_attr  # type: ignore[attr-defined] -- deliberately wrong type

    assert details_for_exception(exc) == [ErrorIdentity(class_name="RuntimeError")]


# ── the error identity ────────────────────────────────────────────────


def test_a_binding_exception_is_named_in_the_nix_vocabulary() -> None:
    """``__module__`` is the discriminator, exactly as translate_nix_exception uses it.

    A bound class's Python name *is* its Nix C++ name, so it goes in
    ``nix_type`` where ``exception_for_nix_type`` can look it up.
    """
    assert identity_for_exception(nanopynix_errors.EvalError("boom")) == ErrorIdentity(nix_type="EvalError")


@pytest.mark.parametrize(
    "exc",
    [
        SettingNotLiveError("pure-eval is read at construction"),
        EvalError("EvalError", "boom"),
        RuntimeError("boom"),
        KeyError("handle 7 not found"),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_everything_else_is_named_in_the_python_vocabulary(exc: Exception) -> None:
    """A class Nix did not raise is named by its own class, whatever it is.

    ``EvalError`` is in this list on purpose: a *translated* NixError is
    nanopynix's class, not Nix's, even though it describes a Nix failure.
    """
    assert identity_for_exception(exc) == ErrorIdentity(class_name=type(exc).__name__)


def test_an_identity_is_always_sent_even_with_no_nix_detail() -> None:
    """Never ``None``. Every exception has a class, and the class is the one
    thing the client can always use."""
    assert details_for_exception(UnresolvedValueError("not resolved")) == [
        ErrorIdentity(class_name="UnresolvedValueError"),
    ]


def test_both_details_ride_the_same_trailer() -> None:
    """Adding the identity must not cost the Nix detail that was already there."""
    exc = EvalError("EvalError", "boom", raw="error: boom", info=_info(traces=1))

    received = _decode(NixStatusDetailsCodec().encode(Status.UNKNOWN, "boom", details_for_exception(exc)))

    assert received.class_name == "EvalError"
    assert received.raw == "error: boom"
    assert received.info == _info(traces=1)


# ── the status code ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (SettingNotLiveError("not live"), Status.FAILED_PRECONDITION),
        (UnresolvedValueError("not resolved"), Status.FAILED_PRECONDITION),
        (ValueError("bad scalar type"), Status.INVALID_ARGUMENT),
        (TypeError("handle 7 is a store, not an eval"), Status.INVALID_ARGUMENT),
        (KeyError("handle 7 not found"), Status.INVALID_ARGUMENT),
        (IndexError("list index out of range: 9"), Status.INVALID_ARGUMENT),
        (RuntimeError("worker executor is unavailable"), Status.UNKNOWN),
        (TimeoutError("primop timed out"), Status.UNKNOWN),
        (EvalError("EvalError", "boom"), Status.UNKNOWN),
    ],
    ids=lambda value: value.name if isinstance(value, Status) else type(value).__name__,
)
def test_the_status_code_names_the_family(exc: Exception, expected: Status) -> None:
    """A client in another language reads the code before it reads anything else."""
    assert status_for_exception(exc) == expected


@pytest.mark.parametrize(
    "exc",
    [
        MissingAttributeError("EvalError", "attribute 'nope' missing"),
        ListIndexError("EvalError", "list index 9 is out of bounds"),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_a_nix_answer_is_not_a_bad_argument(exc: Exception) -> None:
    """``MissingAttributeError`` is also a ``KeyError``, and ``ListIndexError``
    is also an ``IndexError``. Neither is a bad argument to the RPC: Nix
    answered, and the answer was "not there"."""
    assert status_for_exception(exc) == Status.UNKNOWN


# ── decode robustness ─────────────────────────────────────────────────


@pytest.mark.parametrize("details", [None, [], "string", 42, {}, [None, 42], ["not a message"]])
def test_unpack_degrades_to_no_detail_rather_than_raising(details: object) -> None:
    """Degrading is mandatory here: the path is already reporting a failure, so
    a malformed trailer must not replace the caller's Nix error with its own."""
    assert unpack_error_details(details) == ReceivedError()


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

    received = _decode(bytes(proto))
    assert received.raw == "error: boom"
    assert received.info is not None


def test_codec_emits_a_standard_google_rpc_status() -> None:
    """The whole reason for the proto encoding: any language's gRPC tooling
    reads this trailer, not just ours."""
    parsed = RpcStatus.parse(_encode(info=_info()))

    assert parsed.code == Status.UNKNOWN.value
    assert parsed.message == "EvalError: boom"
    assert [detail.type_url for detail in parsed.details] == [
        "type.googleapis.com/nix.common.ErrorIdentity",
        "type.googleapis.com/nix.common.NixErrorInfo",
    ]


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
