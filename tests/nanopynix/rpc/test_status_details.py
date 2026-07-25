"""Unit tests for the ``grpc-status-details-bin`` payload (boundary B).

These exercise the parts of :mod:`nanopynix.rpc._status_details` that a live
RPC round-trip cannot reach: real Nix errors carry at most a couple of trace
frames, so the byte budget never binds and the trimming path never runs. The
budget is not decorative -- the payload rides an HTTP/2 header, where going
over is a connection-level protocol error rather than a truncated field -- so
the trimming order and the give-up branch are pinned here directly.
"""

from __future__ import annotations

import json
import pickle
from typing import Any

import pytest
from grpclib.const import Status

from nanopynix.exceptions import EvalError
from nanopynix.rpc._status_details import (
    MAX_DETAILS_BYTES,
    JsonStatusDetailsCodec,
    details_for_exception,
    pack_error_details,
    unpack_error_details,
)


def _size(payload: object) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _info(*, traces: int = 0, msg: str = "boom") -> dict[str, Any]:
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
    }


# ── pack_error_details ────────────────────────────────────────────────


def test_pack_returns_none_when_there_is_nothing_to_send() -> None:
    assert pack_error_details(raw="", info=None) is None


def test_pack_passes_a_small_payload_through_untouched() -> None:
    info = _info(traces=1)
    packed = pack_error_details(raw="error: boom", info=info)
    assert packed == {"raw": "error: boom", "info": info}


def test_pack_drops_raw_before_it_drops_any_trace_frame() -> None:
    """`raw` is the rendered form of what `info` already holds structurally.

    Spending the budget on it would mean discarding real structure to preserve
    a string the reader can reconstruct, so it goes first.
    """
    info = _info(traces=4)
    oversize_raw = "e" * MAX_DETAILS_BYTES

    packed = pack_error_details(raw=oversize_raw, info=info)

    assert packed is not None
    assert packed["raw"] == ""
    assert len(packed["info"]["traces"]) == 4, "traces must survive a large `raw`"
    assert packed["info"]["truncated"] is True
    assert _size(packed) <= MAX_DETAILS_BYTES


def test_pack_trims_traces_from_the_tail_when_info_alone_is_oversize() -> None:
    info = _info(traces=200)
    assert _size({"raw": "", "info": info}) > MAX_DETAILS_BYTES

    packed = pack_error_details(raw="error: boom", info=info)

    assert packed is not None
    assert _size(packed) <= MAX_DETAILS_BYTES
    kept = packed["info"]["traces"]
    assert 0 < len(kept) < 200
    # Head kept, tail dropped: the innermost frames are the useful ones.
    assert kept == info["traces"][: len(kept)]
    assert packed["info"]["truncated"] is True


def test_pack_clips_raw_to_fit_when_there_is_no_info_to_protect() -> None:
    packed = pack_error_details(raw="e" * (MAX_DETAILS_BYTES * 2), info=None)

    assert packed is not None
    assert _size(packed) <= MAX_DETAILS_BYTES
    assert packed["raw"].endswith("... [truncated]")
    assert len(packed["raw"]) > MAX_DETAILS_BYTES // 2, "clipped, not gutted"


def test_pack_clips_raw_accounting_for_json_escape_inflation() -> None:
    """One control character costs six bytes encoded, hence the bisection."""
    packed = pack_error_details(raw="\x01" * MAX_DETAILS_BYTES, info=None)

    assert packed is not None
    assert _size(packed) <= MAX_DETAILS_BYTES


def test_pack_gives_up_rather_than_emit_an_oversize_header() -> None:
    """A single enormous `msg` is past what the C++ per-string cap allows.

    Dropping the trailer loses the detail; overflowing the header list loses
    the whole response with a protocol error. Prefer the former.
    """
    packed = pack_error_details(raw="", info=_info(msg="m" * (MAX_DETAILS_BYTES * 2)))

    assert packed is None


# ── details_for_exception ─────────────────────────────────────────────


def test_details_for_exception_reads_a_translated_nix_error() -> None:
    exc = EvalError("EvalError", "boom", raw="error: boom", info=_info(traces=1))
    details = details_for_exception(exc)

    assert details is not None
    assert details["raw"] == "error: boom"
    assert details["info"]["msg"] == "boom"


def test_details_for_exception_reads_binding_style_attributes() -> None:
    """The worker's common case: a raw nanobind exception, not a NixError."""

    class _BoundError(RuntimeError):
        pass

    exc = _BoundError("boom")
    exc.raw = "error: boom"  # type: ignore[attr-defined] -- mimics nix_error_info.hh
    exc.info = _info()  # type: ignore[attr-defined] -- mimics nix_error_info.hh

    details = details_for_exception(exc)
    assert details is not None
    assert details["info"]["is_from_expr"] is True


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


# ── unpack_error_details ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "details",
    [None, [], "string", 42, {}, {"raw": None, "info": None}, {"raw": 42, "info": "not a dict"}],
)
def test_unpack_degrades_to_no_detail_rather_than_raising(details: object) -> None:
    """Degrading is mandatory here: the path is already reporting a failure, so
    a malformed trailer must not replace the caller's Nix error with its own."""
    assert unpack_error_details(details) == ("", None)


def test_unpack_round_trips_what_pack_produced() -> None:
    info = _info(traces=2)
    packed = pack_error_details(raw="error: boom", info=info)
    assert unpack_error_details(packed) == ("error: boom", info)


# ── JsonStatusDetailsCodec ────────────────────────────────────────────


def test_codec_round_trips_a_payload() -> None:
    codec = JsonStatusDetailsCodec()
    payload = pack_error_details(raw="error: boom", info=_info(traces=1))
    encoded = codec.encode(Status.UNKNOWN, "boom", payload)
    assert codec.decode(Status.UNKNOWN, "boom", encoded) == payload


@pytest.mark.parametrize("data", [b"\xff\xfe not utf-8", b"{not json", b""])
def test_codec_decode_returns_none_on_malformed_input(data: bytes) -> None:
    codec = JsonStatusDetailsCodec()
    assert codec.decode(Status.UNKNOWN, "boom", data) is None


def test_codec_is_picklable_for_the_forkserver_worker() -> None:
    """The worker runs in a forkserver child, so the instance travels through
    ``multiprocessing`` process args."""
    codec = JsonStatusDetailsCodec()
    revived = pickle.loads(pickle.dumps(codec))  # noqa: S301 -- our own bytes, produced one line above
    assert isinstance(revived, JsonStatusDetailsCodec)
