"""In-memory serde benchmark: old framework vs new pynixd.serde.

Compares serialize and deserialize for IsValidPath and AddTempRoot
(operations 1 and 11 — the most latency-sensitive operations).
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from pyinstrument import Profiler

from pynixd.constants import PROTOCOL_VERSION
from pynixd.operations.add_temp_root import (
    AddTempRootRequest as OldAddTempRootRequest,
)
from pynixd.operations.add_temp_root import (
    AddTempRootResponse as OldAddTempRootResponse,
)
from pynixd.operations.is_valid_path import (
    IsValidPathRequest as OldIsValidPathRequest,
)
from pynixd.operations.is_valid_path import (
    IsValidPathResponse as OldIsValidPathResponse,
)
from pynixd.serde import (
    AddTempRootRequest as NewAddTempRootRequest,
)
from pynixd.serde import (
    AddTempRootResponse as NewAddTempRootResponse,
)
from pynixd.serde import (
    IsValidPathRequest as NewIsValidPathRequest,
)
from pynixd.serde import (
    IsValidPathResponse as NewIsValidPathResponse,
)
from pynixd.serde import StorePath as SerdeStorePath
from pynixd.store_path import StorePath
from pynixd.types.context import ReadContext, WriteContext
from pynixd.wire import BytesReader, BytesWriter, UnixNixReader, UnixNixWriter

# ── Configuration ─────────────────────────────────────────────────

ITERATIONS = 100_000
WARMUP = 1_000
TOLERANCE_SECONDS = 10  # max total time before failing

# ── Pre-built payloads ────────────────────────────────────────────

_OLD_STORE_PATH = StorePath("/nix/store/00000000000000000000000000000000-bench")
_NEW_STORE_PATH = SerdeStorePath(path="/nix/store/00000000000000000000000000000000-bench")


def _build_payloads() -> dict:
    """Construct all benchmark objects once (outside timed loop)."""
    return {
        # IsValidPath
        "old_isv_req": OldIsValidPathRequest(path=_OLD_STORE_PATH),
        "new_isv_req": NewIsValidPathRequest(path=_NEW_STORE_PATH),
        "old_isv_resp": OldIsValidPathResponse(valid=True),
        "new_isv_resp": NewIsValidPathResponse(valid=True),
        # AddTempRoot
        "old_atr_req": OldAddTempRootRequest(path=_OLD_STORE_PATH),
        "new_atr_req": NewAddTempRootRequest(path=_NEW_STORE_PATH),
        "old_atr_resp": OldAddTempRootResponse(value=1),
        "new_atr_resp": NewAddTempRootResponse(value=1),
    }


_PAYLOADS = _build_payloads()


def _serialize(obj, write_ctx: WriteContext | None, is_old: bool) -> bytes:
    """Serialize once to get reference bytes (outside timed loop)."""
    w = BytesWriter()
    ctx = WriteContext(writer=w, version=PROTOCOL_VERSION)
    if is_old:
        # Old serialize is a coroutine, need to run it
        import asyncio

        asyncio.run(obj.serialize(ctx))
    else:
        import asyncio

        asyncio.run(obj.to_writer(ctx))
    return w.get_bytes()


# Pre-serialize all payloads
_OLD_ISV_REQ_BYTES = _serialize(_PAYLOADS["old_isv_req"], None, is_old=True)
_NEW_ISV_REQ_BYTES = _serialize(_PAYLOADS["new_isv_req"], None, is_old=False)
_OLD_ISV_RESP_BYTES = _serialize(_PAYLOADS["old_isv_resp"], None, is_old=True)
_NEW_ISV_RESP_BYTES = _serialize(_PAYLOADS["new_isv_resp"], None, is_old=False)
_OLD_ATR_REQ_BYTES = _serialize(_PAYLOADS["old_atr_req"], None, is_old=True)
_NEW_ATR_REQ_BYTES = _serialize(_PAYLOADS["new_atr_req"], None, is_old=False)
_OLD_ATR_RESP_BYTES = _serialize(_PAYLOADS["old_atr_resp"], None, is_old=True)
_NEW_ATR_RESP_BYTES = _serialize(_PAYLOADS["new_atr_resp"], None, is_old=False)


# ── Bench helpers ─────────────────────────────────────────────────


async def _bench_serialize(obj, is_old: bool) -> float:
    """Serialize ``obj`` ITERATIONS times, return microseconds per iteration."""
    for _ in range(WARMUP):
        w = BytesWriter()
        ctx = WriteContext(writer=w, version=PROTOCOL_VERSION)
        if is_old:
            await obj.serialize(ctx)
        else:
            await obj.to_writer(ctx)

    t0 = time.perf_counter()
    for _ in range(ITERATIONS):
        w = BytesWriter()
        ctx = WriteContext(writer=w, version=PROTOCOL_VERSION)
        if is_old:
            await obj.serialize(ctx)
        else:
            await obj.to_writer(ctx)
    elapsed = time.perf_counter() - t0
    return (elapsed / ITERATIONS) * 1_000_000  # µs


async def _bench_deserialize_request(data: bytes, cls, is_old: bool) -> float:
    """Deserialize a REQUEST body ITERATIONS times. ``data`` is body-only (op already stripped)."""
    for _ in range(WARMUP):
        r = BytesReader(data)
        if is_old:
            await cls.deserialize(ReadContext(reader=r, version=PROTOCOL_VERSION))
        else:
            await cls.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    t0 = time.perf_counter()
    for _ in range(ITERATIONS):
        r = BytesReader(data)
        if is_old:
            await cls.deserialize(ReadContext(reader=r, version=PROTOCOL_VERSION))
        else:
            await cls.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    elapsed = time.perf_counter() - t0
    return (elapsed / ITERATIONS) * 1_000_000  # µs


async def _bench_deserialize_response(data: bytes, cls, is_old: bool) -> float:
    """Deserialize a RESPONSE (logs + body) ITERATIONS times."""
    for _ in range(WARMUP):
        r = BytesReader(data)
        if is_old:
            await cls.deserialize(ReadContext(reader=r, version=PROTOCOL_VERSION))
        else:
            await cls.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    t0 = time.perf_counter()
    for _ in range(ITERATIONS):
        r = BytesReader(data)
        if is_old:
            await cls.deserialize(ReadContext(reader=r, version=PROTOCOL_VERSION))
        else:
            await cls.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    elapsed = time.perf_counter() - t0
    return (elapsed / ITERATIONS) * 1_000_000  # µs


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.parametrize("operation", ["IsValidPath", "AddTempRoot"])
@pytest.mark.asyncio
async def test_bench_serialize_request(operation: str):
    """Benchmark request serialization."""
    if operation == "IsValidPath":
        old_obj = _PAYLOADS["old_isv_req"]
        new_obj = _PAYLOADS["new_isv_req"]
    else:
        old_obj = _PAYLOADS["old_atr_req"]
        new_obj = _PAYLOADS["new_atr_req"]

    old_us = await _bench_serialize(old_obj, is_old=True)
    new_us = await _bench_serialize(new_obj, is_old=False)
    ratio = new_us / old_us if old_us > 0 else float("inf")

    print(f"\n{operation} REQUEST serialize:")
    print(f"  old: {old_us:.2f} µs/iter")
    print(f"  new: {new_us:.2f} µs/iter")
    print(f"  ratio (new/old): {ratio:.2f}x")


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.parametrize("operation", ["IsValidPath", "AddTempRoot"])
@pytest.mark.asyncio
async def test_bench_deserialize_request(operation: str):
    """Benchmark request deserialization (body-only, op pre-stripped)."""
    if operation == "IsValidPath":
        old_data = _OLD_ISV_REQ_BYTES[8:]  # strip 8-byte op
        new_data = _NEW_ISV_REQ_BYTES[8:]
        old_cls = OldIsValidPathRequest
        new_cls = NewIsValidPathRequest
    else:
        old_data = _OLD_ATR_REQ_BYTES[8:]
        new_data = _NEW_ATR_REQ_BYTES[8:]
        old_cls = OldAddTempRootRequest
        new_cls = NewAddTempRootRequest

    old_us = await _bench_deserialize_request(old_data, old_cls, is_old=True)
    new_us = await _bench_deserialize_request(new_data, new_cls, is_old=False)
    ratio = new_us / old_us if old_us > 0 else float("inf")

    print(f"\n{operation} REQUEST deserialize:")
    print(f"  old: {old_us:.2f} µs/iter")
    print(f"  new: {new_us:.2f} µs/iter")
    print(f"  ratio (new/old): {ratio:.2f}x")


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.parametrize("operation", ["IsValidPath", "AddTempRoot"])
@pytest.mark.asyncio
async def test_bench_serialize_response(operation: str):
    """Benchmark response serialization (logs + body)."""
    if operation == "IsValidPath":
        old_obj = _PAYLOADS["old_isv_resp"]
        new_obj = _PAYLOADS["new_isv_resp"]
    else:
        old_obj = _PAYLOADS["old_atr_resp"]
        new_obj = _PAYLOADS["new_atr_resp"]

    old_us = await _bench_serialize(old_obj, is_old=True)
    new_us = await _bench_serialize(new_obj, is_old=False)
    ratio = new_us / old_us if old_us > 0 else float("inf")

    print(f"\n{operation} RESPONSE serialize:")
    print(f"  old: {old_us:.2f} µs/iter")
    print(f"  new: {new_us:.2f} µs/iter")
    print(f"  ratio (new/old): {ratio:.2f}x")


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.parametrize("operation", ["IsValidPath", "AddTempRoot"])
@pytest.mark.asyncio
async def test_bench_deserialize_response(operation: str):
    """Benchmark response deserialization (logs + body)."""
    if operation == "IsValidPath":
        old_data = _OLD_ISV_RESP_BYTES
        new_data = _NEW_ISV_RESP_BYTES
        old_cls = OldIsValidPathResponse
        new_cls = NewIsValidPathResponse
    else:
        old_data = _OLD_ATR_RESP_BYTES
        new_data = _NEW_ATR_RESP_BYTES
        old_cls = OldAddTempRootResponse
        new_cls = NewAddTempRootResponse

    old_us = await _bench_deserialize_response(old_data, old_cls, is_old=True)
    new_us = await _bench_deserialize_response(new_data, new_cls, is_old=False)
    ratio = new_us / old_us if old_us > 0 else float("inf")

    print(f"\n{operation} RESPONSE deserialize:")
    print(f"  old: {old_us:.2f} µs/iter")
    print(f"  new: {new_us:.2f} µs/iter")
    print(f"  ratio (new/old): {ratio:.2f}x")


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.asyncio
async def test_profile_new_serialize():
    """Profile JUST the new serde serialize loop (IsValidPath request)."""
    obj = _PAYLOADS["new_isv_req"]

    # Warmup
    for _ in range(1_000):
        w = BytesWriter()
        await obj.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))

    profiler = Profiler(async_mode="enabled")
    profiler.start()
    for _ in range(10_000):
        w = BytesWriter()
        await obj.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    profiler.stop()

    profiler.print(show_all=True)


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.asyncio
async def test_profile_new_deserialize():
    """Profile JUST the new serde deserialize loop (IsValidPath request)."""
    data = _NEW_ISV_REQ_BYTES[8:]  # body after op

    # Warmup
    for _ in range(1_000):
        r = BytesReader(data)
        await NewIsValidPathRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    profiler = Profiler(async_mode="enabled")
    profiler.start()
    for _ in range(10_000):
        r = BytesReader(data)
        await NewIsValidPathRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    profiler.stop()

    profiler.print(show_all=True)


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.asyncio
async def test_profile_new_serialize_response():
    """Profile JUST the new serde serialize loop (IsValidPath response)."""
    obj = _PAYLOADS["new_isv_resp"]

    for _ in range(1_000):
        w = BytesWriter()
        await obj.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))

    profiler = Profiler(async_mode="enabled")
    profiler.start()
    for _ in range(10_000):
        w = BytesWriter()
        await obj.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
    profiler.stop()

    profiler.print(show_all=True)


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.asyncio
async def test_profile_new_deserialize_response():
    """Profile JUST the new serde deserialize loop (IsValidPath response)."""
    data = _NEW_ISV_RESP_BYTES

    for _ in range(1_000):
        r = BytesReader(data)
        await NewIsValidPathResponse.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

    profiler = Profiler(async_mode="enabled")
    profiler.start()
    for _ in range(10_000):
        r = BytesReader(data)
        await NewIsValidPathResponse.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
    profiler.stop()

    profiler.print(show_all=True)





@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.asyncio
async def test_unix_socket_is_valid_path_new():
    """Unix socket round-trip: new framework IsValidPath."""
    sock_path = f"/tmp/pynixd-bench-isv-new-{os.getpid()}.sock"
    req_obj = _PAYLOADS["new_isv_req"]
    resp_cls = NewIsValidPathResponse

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        r = UnixNixReader(reader=reader, identifier="srv")
        w = UnixNixWriter(writer=writer, identifier="srv")
        while True:
            try:
                op = await r.read_uint64()
            except EOFError:
                break
            if op == 1:
                await NewIsValidPathRequest.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
                resp = NewIsValidPathResponse(valid=True)
                await resp.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
                await w.drain()

    server = await asyncio.start_unix_server(handler, sock_path)
    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)
        r = UnixNixReader(reader=reader, identifier="cli")
        w = UnixNixWriter(writer=writer, identifier="cli")

        for _ in range(50):  # warmup
            await req_obj.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
            await w.drain()
            await resp_cls.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))

        num_iters = 1_000
        profiler = Profiler(async_mode="enabled")
        profiler.start()
        for _ in range(num_iters):
            await req_obj.to_writer(WriteContext(writer=w, version=PROTOCOL_VERSION))
            await w.drain()
            await resp_cls.from_reader(ReadContext(reader=r, version=PROTOCOL_VERSION))
        profiler.stop()
        profiler.print(show_all=True)

        writer.close()
    finally:
        server.close()
        await server.wait_closed()
        from contextlib import suppress

        with suppress(OSError):
            os.unlink(sock_path)  # noqa: PTH108


@pytest.mark.no_profile
@pytest.mark.bench
@pytest.mark.asyncio
async def test_unix_socket_is_valid_path_old():
    """Unix socket round-trip: old framework IsValidPath."""
    sock_path = f"/tmp/pynixd-bench-isv-old-{os.getpid()}.sock"
    req_obj = _PAYLOADS["old_isv_req"]
    resp_cls = OldIsValidPathResponse

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        r = UnixNixReader(reader=reader, identifier="srv")
        w = UnixNixWriter(writer=writer, identifier="srv")
        while True:
            try:
                op = await r.read_uint64()
            except EOFError:
                break
            if op == 1:
                await OldIsValidPathRequest.deserialize(ReadContext(reader=r, version=PROTOCOL_VERSION))
                resp = OldIsValidPathResponse(valid=True)
                await resp.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
                await w.drain()

    server = await asyncio.start_unix_server(handler, sock_path)
    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)
        r = UnixNixReader(reader=reader, identifier="cli")
        w = UnixNixWriter(writer=writer, identifier="cli")

        for _ in range(50):
            await req_obj.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
            await w.drain()
            await resp_cls.deserialize(ReadContext(reader=r, version=PROTOCOL_VERSION))

        num_iters = 1_000
        profiler = Profiler(async_mode="enabled")
        profiler.start()
        for _ in range(num_iters):
            await req_obj.serialize(WriteContext(writer=w, version=PROTOCOL_VERSION))
            await w.drain()
            await resp_cls.deserialize(ReadContext(reader=r, version=PROTOCOL_VERSION))
        profiler.stop()
        profiler.print(show_all=True)

        writer.close()
    finally:
        server.close()
        await server.wait_closed()
        from contextlib import suppress

        with suppress(OSError):
            os.unlink(sock_path)  # noqa: PTH108
