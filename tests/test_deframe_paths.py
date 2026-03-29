"""Tests for extracting store paths during AddMultipleToStore streaming.

Verifies that FramedReader can deframe + parse PathInfo + stream NAR
without buffering the entire framed payload, and that the store paths
are correctly extracted and returned.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest
from conftest import NIX_BIN

from pynixd import wire
from pynixd.operations.base import PathInfo
from pynixd.store import LocalSocketStore, LocalSubprocessStore, Store
from pynixd.wire import (
    FramedReader,
    FramedWriter,
    UnixNixReader,
    UnixNixWriter,
)

log = logging.getLogger(__name__)

DEST_STORE = "/tmp/pynixd-test-deframe-dst"


@pytest.fixture
async def src_store() -> LocalSocketStore:
    s = LocalSocketStore(id="system")
    yield s
    await s.close()


@pytest.fixture
async def dst_store() -> LocalSubprocessStore:
    os.makedirs(DEST_STORE, exist_ok=True)
    s = LocalSubprocessStore(store_path=DEST_STORE, id="deframe-dst", nix_bin=NIX_BIN)
    yield s
    await s.close()


async def _pick_self_contained_paths(
    store: Store,
    count: int,
) -> list[tuple[str, PathInfo, bytes]]:
    """Pick `count` small self-contained paths from the store."""
    all_paths = await store.query_all_valid_paths()
    picked: list[tuple[str, PathInfo, bytes]] = []
    for p in sorted(all_paths):
        if len(picked) >= count:
            break
        if p.endswith(".drv"):
            continue
        info = await store.query_path_info(p)
        if info and 0 < info.nar_size < 100_000:
            if info.references - {p}:
                continue
            nar = await store.buffer_nar_from_path(p)
            if nar:
                picked.append((p, info, nar))
    assert len(picked) == count, f"Need {count} paths, found {len(picked)}"
    return picked


# ── Unit test: FramedReader round-trip ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_framed_reader_roundtrip() -> None:
    """FramedReader can read wire protocol values written by FramedWriter."""
    # Create an in-memory pipe via asyncio streams
    rd, wr = await _make_pipe()
    daemon_rd = UnixNixReader(rd)
    daemon_wr = UnixNixWriter(wr)

    # Write framed data: a uint64 count + two strings
    fw = FramedWriter(daemon_wr, chunk_size=32)  # small chunks to test reassembly
    fw.write_uint64(2)
    fw.write_string("/nix/store/aaaa-hello")
    fw.write_string("/nix/store/bbbb-world")
    await fw.finalize()

    # Read it back via FramedReader (FramedReader is-a NixReader)
    fr = FramedReader(daemon_rd)

    count = await fr.read_uint64()
    assert count == 2

    s1 = await fr.read_string()
    assert s1 == "/nix/store/aaaa-hello"

    s2 = await fr.read_string()
    assert s2 == "/nix/store/bbbb-world"

    await fr.drain_remaining()
    assert fr.at_eof

    wr.close()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_framed_reader_with_pathinfo_and_nar(
    src_store: LocalSocketStore,
) -> None:
    """FramedReader can parse PathInfo + NAR from framed data (the
    exact format used inside AddMultipleToStore)."""
    picked = await _pick_self_contained_paths(src_store, 2)

    # Build framed payload matching AddMultipleToStore inner format
    rd, wr = await _make_pipe()
    daemon_rd = UnixNixReader(rd)
    daemon_wr = UnixNixWriter(wr)

    fw = FramedWriter(daemon_wr, chunk_size=4096)
    fw.write_uint64(len(picked))  # count
    for _path, info, nar_data in picked:
        # Write keyed PathInfo
        fw.write_string(info.path)
        fw.write_string(info.deriver)
        fw.write_string(info.nar_hash)
        fw.write_string_set(info.references)
        fw.write_uint64(info.registration_time)
        fw.write_uint64(info.nar_size)
        fw.write_uint64(info.ultimate)
        fw.write_string_set(info.sigs)
        fw.write_string(info.ca)
        fw.write(nar_data)  # raw NAR (self-delimiting)
    await fw.finalize()

    # Now deframe and parse — this is what AddMultipleToStoreRequest.forward does
    fr = FramedReader(daemon_rd)

    count = await fr.read_uint64()
    assert count == 2

    extracted_paths: list[str] = []
    for _ in range(count):
        info = await PathInfo.from_reader_keyed(fr)
        extracted_paths.append(info.path)
        # Stream NAR to /dev/null (discard)
        await wire.discard_nar(fr)

    assert extracted_paths == [p for p, _, _ in picked]
    await fr.drain_remaining()
    assert fr.at_eof

    wr.close()


# ── Integration test: streaming with real stores ──────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_add_multiple_streaming_returns_paths(
    src_store: LocalSocketStore,
    dst_store: LocalSubprocessStore,
) -> None:
    """add_multiple_to_store_streaming extracts paths while forwarding to store."""
    picked = await _pick_self_contained_paths(src_store, 2)

    # Build framed payload matching what a nix client sends
    payload_rd, payload_wr = await _make_pipe()
    daemon_payload_rd = UnixNixReader(payload_rd)

    async def _write_payload() -> None:
        daemon_wr = UnixNixWriter(payload_wr)
        # Write the AddMultipleToStore request prefix
        daemon_wr.write_uint64(0)  # repair
        daemon_wr.write_uint64(1)  # dont_check_sigs

        # Write framed inner data
        fw = FramedWriter(daemon_wr, chunk_size=4096)
        fw.write_uint64(len(picked))
        for _path, info, nar_data in picked:
            fw.write_string(info.path)
            fw.write_string(info.deriver)
            fw.write_string(info.nar_hash)
            fw.write_string_set(info.references)
            fw.write_uint64(info.registration_time)
            fw.write_uint64(info.nar_size)
            fw.write_uint64(info.ultimate)
            fw.write_string_set(info.sigs)
            fw.write_string(info.ca)
            fw.write(nar_data)
        await fw.finalize()
        await daemon_wr.drain()

    # Write payload in background, consume with store streaming
    write_task = asyncio.create_task(_write_payload())

    # Skip if paths already present
    paths_set = {p for p, _, _ in picked}
    valid_before = await dst_store.query_valid_paths(paths_set)
    if valid_before == paths_set:
        pytest.skip("Paths already in dest store")

    paths = await dst_store.add_multiple_to_store_streaming(daemon_payload_rd)

    await write_task

    # Verify paths were extracted
    assert len(paths) == 2
    expected = [p for p, _, _ in picked]
    assert paths == expected

    # Verify paths actually arrived in the store
    for path, info, _ in picked:
        dst_info = await dst_store.query_path_info(path)
        assert dst_info is not None, f"{path} missing in dest store"
        assert dst_info.nar_hash == info.nar_hash
        assert dst_info.nar_size == info.nar_size

    log.info("Extracted paths during streaming: %s", paths)

    payload_wr.close()


# ── Helpers ───────────────────────────────────────────────────────────


async def _make_pipe() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Create an in-memory asyncio stream pair via connected sockets."""
    server_ready = asyncio.Event()
    conns: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

    async def _on_connect(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        conns.append((reader, writer))
        server_ready.set()

    server = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
    addr = server.sockets[0].getsockname()
    client_rd, client_wr = await asyncio.open_connection(addr[0], addr[1])
    await server_ready.wait()
    server_rd, server_wr = conns[0]
    server.close()

    # client_wr → server_rd is our pipe
    return server_rd, client_wr
