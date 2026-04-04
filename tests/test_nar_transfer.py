"""Tests for NAR transfer between stores using AddToStoreNar and AddMultipleToStore.

Uses LocalSocketStore (system store) as source — it already has paths.
Uses LocalSubprocessStore as destination — fresh empty store.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from conftest import NIX_BIN, rmtree_robust

from pynixd.operations.base import PathInfo
from pynixd.operations.store_mutations import (
    AddToStoreNarRequest,
)
from pynixd.store import (
    LocalSocketStore,
    LocalSubprocessStore,
    SSHSubprocessStore,
    Store,
)
from pynixd.wire import UnixNixReader, UnixNixWriter

log = structlog.get_logger(__name__)

DEST_STORE = Path("/tmp/pynixd-test-nar-dst")


@pytest.fixture
async def src_store() -> AsyncIterator[LocalSocketStore]:
    """System store as source — has paths already."""
    s = LocalSocketStore(id="system")
    yield s
    await s.close()


@pytest.fixture
async def dst_store() -> AsyncIterator[LocalSubprocessStore]:
    """Fresh empty store as destination."""
    rmtree_robust(DEST_STORE)
    os.makedirs(DEST_STORE, exist_ok=True)
    s = LocalSubprocessStore(store_path=DEST_STORE, id="dst", nix_bin=str(NIX_BIN))
    yield s
    await s.close()


async def _pick_a_path(store: Store, need_no_refs: bool = False) -> str:
    """Pick an arbitrary valid path from the store.

    If need_no_refs=True, pick a path with no references (self-contained).
    """
    all_paths = await store.query_all_valid_paths()
    assert all_paths, "System store has no paths?!"
    for p in sorted(all_paths):
        if p.endswith(".drv"):
            continue  # skip derivations, they have drv references
        info = await store.query_path_info(p)
        if info and info.nar_size > 0 and info.nar_size < 100_000:
            if need_no_refs and info.references - {p}:
                continue  # has references to other paths (self-ref is ok)
            return p
    pytest.fail("Could not find a suitable path in system store")


async def _get_path_info_and_nar(
    store: Store,
    path: str,
) -> tuple[PathInfo, bytes]:
    """Query PathInfo and fetch NAR for a path."""
    info = await store.query_path_info(path)
    assert info is not None, f"Path {path} not valid in store"
    nar = await store.buffer_nar_from_path(path)
    assert nar, f"Empty NAR for {path}"
    return info, nar


# ── AddToStoreNar ────────────────────────────────────────────────────


# @pytest.mark.skip(
#     reason="test calls non-existent Connection.add_to_store_nar() - needs rewrite"
# )
@pytest.mark.timeout(30)
async def test_add_to_store_nar(
    src_store: LocalSocketStore,
    dst_store: LocalSubprocessStore,
) -> None:
    """Copy a single path from system store to dest using AddToStoreNar."""
    path = await _pick_a_path(src_store, need_no_refs=True)
    info, nar_data = await _get_path_info_and_nar(src_store, path)

    log.info(
        "AddToStoreNar: path=%s nar_size=%d nar_hash=%s refs=%s",
        info.path,
        info.nar_size,
        info.nar_hash,
        info.references,
    )

    async with dst_store.transfer_conn() as dst:
        # Send via AddToStoreNar (manual protocol exchange)
        from pynixd.operations.base import EmptyResponse
        from pynixd.protocol import Op

        dst.w.write_uint64(Op.AddToStoreNar)
        request = AddToStoreNarRequest(
            info=info,
            repair=0,
            dont_check_sigs=1,
        )
        await request.to_writer(dst.w, dst.version)

        # Write nar_data as framed
        from pynixd.wire import FramedWriter

        fw = FramedWriter(dst.w)
        fw.write(nar_data)
        await fw.finalize()

        # Read stderr and response
        from pynixd import stderr as nix_stderr

        await nix_stderr.drain(dst.r)
        await EmptyResponse.from_reader(dst.r, dst.version)

    # Verify it arrived
    valid_after = await dst_store.query_valid_paths({path})
    assert path in valid_after, "Path not in dest store after AddToStoreNar"

    # Verify content matches
    dst_info = await dst_store.query_path_info(path)
    assert dst_info is not None
    assert dst_info.nar_hash == info.nar_hash
    assert dst_info.nar_size == info.nar_size


# ── AddMultipleToStore ───────────────────────────────────────────────


@pytest.mark.timeout(30)
async def test_add_multiple_to_store_single(
    src_store: LocalSocketStore,
    dst_store: LocalSubprocessStore,
) -> None:
    """Copy a single path from system store to dest using AddMultipleToStore."""
    path = await _pick_a_path(src_store, need_no_refs=True)
    info, nar_data = await _get_path_info_and_nar(src_store, path)

    log.info(
        "AddMultipleToStore(1): path=%s nar_size=%d nar_bytes=%d refs=%s",
        info.path,
        info.nar_size,
        len(nar_data),
        info.references,
    )

    # Create a pipe for streaming AddMultipleToStore
    server_ready = asyncio.Event()
    conns: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

    async def _on_connect(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conns.append((reader, writer))
        server_ready.set()

    srv = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
    addr = srv.sockets[0].getsockname()
    client_rd, client_wr = await asyncio.open_connection(addr[0], addr[1])
    await server_ready.wait()
    srv_rd, srv_wr = conns[0]
    srv.close()
    daemon_payload_rd = UnixNixReader(client_rd)

    reader_done = asyncio.Event()

    async def _write_payload() -> None:
        daemon_wr = UnixNixWriter(srv_wr)
        w = daemon_wr
        w.write_uint64(0)  # repair
        w.write_uint64(1)  # dont_check_sigs
        await w.drain()

        # The rest is framed
        fw = w.framed()
        fw.write_uint64(1)  # count
        fw.write_string(info.path)
        fw.write_string(info.deriver)
        fw.write_string(info.nar_hash)
        fw.write_string_set(info.references)
        fw.write_uint64(info.registration_time)
        fw.write_uint64(info.nar_size)
        fw.write_uint64(info.ultimate)
        fw.write_string_set(info.sigs)
        fw.write_string(info.ca)
        fw.write(nar_data)  # raw NAR, no length prefix
        await fw.finalize()
        await daemon_wr.drain()

        # Wait for reader to finish before closing
        await reader_done.wait()
        srv_wr.close()
        await srv_wr.wait_closed()
        client_wr.close()
        await client_wr.wait_closed()

    write_task = asyncio.create_task(_write_payload())
    try:
        paths = await dst_store.add_multiple_to_store_streaming(daemon_payload_rd)
    finally:
        reader_done.set()
    await write_task

    # Verify it arrived
    assert path in paths, "Path not extracted during AddMultipleToStore streaming"
    valid_after = await dst_store.query_valid_paths({path})
    assert path in valid_after, "Path not in dest store after AddMultipleToStore"

    dst_info = await dst_store.query_path_info(path)
    assert dst_info is not None
    assert dst_info.nar_hash == info.nar_hash


@pytest.mark.timeout(30)
async def test_add_multiple_to_store_two_paths(
    src_store: LocalSocketStore,
    dst_store: LocalSubprocessStore,
) -> None:
    """Copy two paths from system store to dest using AddMultipleToStore."""
    all_paths = await src_store.query_all_valid_paths()
    # Pick two small self-contained paths (no external references)
    picked: list[tuple[str, PathInfo, bytes]] = []
    for p in sorted(all_paths):
        if len(picked) >= 2:
            break
        if p.endswith(".drv"):
            continue
        info = await src_store.query_path_info(p)
        if info and 0 < info.nar_size < 100_000:
            if info.references - {p}:
                continue
            nar = await src_store.buffer_nar_from_path(p)
            if nar:
                picked.append((p, info, nar))

    assert len(picked) == 2, (
        f"Could not find 2 small self-contained paths (found {len(picked)})"
    )

    # Create a pipe for streaming AddMultipleToStore
    server_ready = asyncio.Event()
    conns: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

    async def _on_connect(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conns.append((reader, writer))
        server_ready.set()

    srv = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
    addr = srv.sockets[0].getsockname()
    client_rd, client_wr = await asyncio.open_connection(addr[0], addr[1])
    await server_ready.wait()
    srv_rd, srv_wr = conns[0]
    srv.close()
    daemon_payload_rd = UnixNixReader(client_rd)

    reader_done = asyncio.Event()

    async def _write_payload() -> None:
        daemon_wr = UnixNixWriter(srv_wr)
        w = daemon_wr
        w.write_uint64(0)  # repair
        w.write_uint64(1)  # dont_check_sigs
        await w.drain()

        # The rest is framed
        fw = w.framed()
        fw.write_uint64(2)  # count

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

        # Wait for reader to finish before closing
        await reader_done.wait()
        srv_wr.close()
        await srv_wr.wait_closed()
        client_wr.close()
        await client_wr.wait_closed()

    write_task = asyncio.create_task(_write_payload())
    try:
        paths = await dst_store.add_multiple_to_store_streaming(daemon_payload_rd)
    finally:
        reader_done.set()
    await write_task

    # Verify both arrived
    extracted = {p for p, _, _ in picked}
    assert set(paths) == extracted, f"Paths mismatch: got {paths}, expected {extracted}"
    valid_after = await dst_store.query_valid_paths(extracted)
    for p, _, _ in picked:
        assert p in valid_after, f"Path {p} not in dest after AddMultipleToStore"


# ── copy_paths (streaming AddMultipleToStore) ─────────────────────


COPY_DEST_STORE = Path("/tmp/pynixd-test-nar-copy")


@pytest.fixture
async def copy_dst_store() -> AsyncIterator[LocalSubprocessStore]:
    """Separate dest store for copy_paths tests."""
    rmtree_robust(COPY_DEST_STORE)
    os.makedirs(COPY_DEST_STORE, exist_ok=True)
    s = LocalSubprocessStore(
        store_path=COPY_DEST_STORE, id="copy-dst", nix_bin=str(NIX_BIN)
    )
    yield s
    await s.close()


@pytest.mark.timeout(30)
async def test_copy_paths_single(
    src_store: LocalSocketStore,
    copy_dst_store: LocalSubprocessStore,
) -> None:
    """Copy a single path via copy_paths (NarFromPath → AddMultipleToStore)."""
    path = await _pick_a_path(src_store, need_no_refs=True)
    info = await src_store.query_path_info(path)
    assert info is not None

    log.info(
        "copy_paths(1): path=%s nar_size=%d nar_hash=%s",
        info.path,
        info.nar_size,
        info.nar_hash,
    )

    await copy_dst_store.stream_paths_store_to_store(src_store, [(path, info)])

    # Verify it arrived
    valid_after = await copy_dst_store.query_valid_paths({path})
    assert path in valid_after, "Path not in dest after copy_paths"

    dst_info = await copy_dst_store.query_path_info(path)
    assert dst_info is not None
    assert dst_info.nar_hash == info.nar_hash
    assert dst_info.nar_size == info.nar_size


@pytest.mark.timeout(30)
async def test_copy_paths_multiple(
    src_store: LocalSocketStore,
    copy_dst_store: LocalSubprocessStore,
) -> None:
    """Copy multiple paths via copy_paths in one AddMultipleToStore call."""
    all_paths = await src_store.query_all_valid_paths()
    picked: list[tuple[str, PathInfo]] = []
    for p in sorted(all_paths):
        if len(picked) >= 3:
            break
        if p.endswith(".drv"):
            continue
        info = await src_store.query_path_info(p)
        if info and 0 < info.nar_size < 100_000:
            if info.references - {p}:
                continue
            picked.append((p, info))

    assert len(picked) >= 2, f"Need at least 2 paths, found {len(picked)}"

    await copy_dst_store.stream_paths_store_to_store(src_store, picked)

    # Verify all arrived
    paths = {p for p, _ in picked}
    valid_after = await copy_dst_store.query_valid_paths(paths)
    for p, info in picked:
        assert p in valid_after, f"Path {p} not in dest after copy_paths"

        dst_info = await copy_dst_store.query_path_info(p)
        assert dst_info is not None
        assert dst_info.nar_hash == info.nar_hash


# ── pipe_nar_from (streaming) ──────────────────────────────────────


STREAM_DEST_STORE = Path("/tmp/pynixd-test-nar-stream")


@pytest.fixture
async def stream_dst_store() -> AsyncIterator[LocalSubprocessStore]:
    """Separate dest store for streaming tests."""
    rmtree_robust(STREAM_DEST_STORE)
    os.makedirs(STREAM_DEST_STORE, exist_ok=True)
    s = LocalSubprocessStore(
        store_path=STREAM_DEST_STORE, id="stream-dst", nix_bin=str(NIX_BIN)
    )
    yield s
    await s.close()


@pytest.mark.timeout(30)
async def test_pipe_nar_from_single(
    src_store: LocalSocketStore,
    stream_dst_store: LocalSubprocessStore,
) -> None:
    """Stream a single path via pipe_nar_from (NarFromPath → AddToStoreNar)."""
    path = await _pick_a_path(src_store, need_no_refs=True)
    info = await src_store.query_path_info(path)
    assert info is not None

    log.info(
        "pipe_nar_from: path=%s nar_size=%d nar_hash=%s",
        info.path,
        info.nar_size,
        info.nar_hash,
    )

    await stream_dst_store.pipe_nar_from(src_store, path, info)

    # Verify it arrived
    valid_after = await stream_dst_store.query_valid_paths({path})
    assert path in valid_after, "Path not in dest after pipe_nar_from"

    dst_info = await stream_dst_store.query_path_info(path)
    assert dst_info is not None
    assert dst_info.nar_hash == info.nar_hash
    assert dst_info.nar_size == info.nar_size


@pytest.mark.timeout(30)
async def test_pipe_nar_from_multiple(
    src_store: LocalSocketStore,
    stream_dst_store: LocalSubprocessStore,
) -> None:
    """Stream multiple paths via pipe_nar_from in sequence."""
    all_paths = await src_store.query_all_valid_paths()
    picked: list[tuple[str, PathInfo]] = []
    for p in sorted(all_paths):
        if len(picked) >= 3:
            break
        if p.endswith(".drv"):
            continue
        info = await src_store.query_path_info(p)
        if info and 0 < info.nar_size < 100_000:
            if info.references - {p}:
                continue
            picked.append((p, info))

    assert len(picked) >= 2, f"Need at least 2 paths, found {len(picked)}"

    for path, info in picked:
        await stream_dst_store.pipe_nar_from(src_store, path, info)

    paths = {p for p, _ in picked}
    valid_after = await stream_dst_store.query_valid_paths(paths)
    for p, _ in picked:
        assert p in valid_after, f"Path {p} not in dest after pipe_nar_from"


# ── nixbuild.net streaming (protocol 1.32) ────────────────────────


@pytest.fixture
async def nixbuild_store() -> AsyncIterator[SSHSubprocessStore]:
    """nixbuild.net store (protocol 1.32)."""
    from environs import Env

    env = Env()
    username = env.str("USER", "root")
    s = SSHSubprocessStore(
        host="eu.nixbuild.net",
        username=username,
        id="nixbuild",
        max_builds=2,
    )
    yield s
    await s.close()


@pytest.mark.nixbuild
@pytest.mark.timeout(60)
async def test_copy_paths_to_nixbuild(
    src_store: LocalSocketStore,
    nixbuild_store: SSHSubprocessStore,
) -> None:
    """Push paths from system store to nixbuild via copy_paths (proto 1.32)."""
    path = await _pick_a_path(src_store, need_no_refs=True)
    info = await src_store.query_path_info(path)
    assert info is not None

    log.info(
        "copy_paths → nixbuild: path=%s nar_size=%d",
        info.path,
        info.nar_size,
    )

    await nixbuild_store.stream_paths_store_to_store(src_store, [(path, info)])

    # Verify it arrived
    valid = await nixbuild_store.query_valid_paths({path})
    assert path in valid, "Path not on nixbuild after copy_paths"

    nb_info = await nixbuild_store.query_path_info(path)
    assert nb_info is not None
    assert nb_info.nar_hash == info.nar_hash


@pytest.mark.nixbuild
@pytest.mark.timeout(60)
async def test_copy_paths_roundtrip_nixbuild(
    src_store: LocalSocketStore,
    nixbuild_store: SSHSubprocessStore,
) -> None:
    """Push paths to nixbuild, then pull them back via copy_paths (proto 1.32)."""
    roundtrip_store = Path("/tmp/pynixd-test-nar-roundtrip")
    rmtree_robust(roundtrip_store)
    os.makedirs(roundtrip_store, exist_ok=True)
    roundtrip_store_instance = LocalSubprocessStore(
        store_path=roundtrip_store,
        id="roundtrip",
        nix_bin=str(NIX_BIN),
    )

    try:
        picked: list[tuple[str, PathInfo]] = []
        all_paths = await src_store.query_all_valid_paths()
        for p in sorted(all_paths):
            if len(picked) >= 2:
                break
            if p.endswith(".drv"):
                continue
            info = await src_store.query_path_info(p)
            if info and 0 < info.nar_size < 100_000:
                if info.references - {p}:
                    continue
                picked.append((p, info))

        assert len(picked) >= 2, f"Need 2 paths, found {len(picked)}"

        # Push to nixbuild
        await nixbuild_store.stream_paths_store_to_store(src_store, picked)
        log.info("pushed_paths_to_nixbuild", count=len(picked))

        # Pull back from nixbuild to fresh local store
        await roundtrip_store_instance.stream_paths_store_to_store(
            nixbuild_store, picked
        )
        log.info("pulled_paths_from_nixbuild", count=len(picked))

        # Verify all arrived with matching hashes
        for path, orig_info in picked:
            rt_info = await roundtrip_store_instance.query_path_info(path)
            assert rt_info is not None, f"{path} missing after roundtrip"
            assert rt_info.nar_hash == orig_info.nar_hash
            assert rt_info.nar_size == orig_info.nar_size
    finally:
        await roundtrip_store_instance.close()
