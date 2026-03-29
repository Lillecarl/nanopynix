"""Tests for NAR transfer between stores using AddToStoreNar and AddMultipleToStore.

Uses LocalSocketStore (system store) as source — it already has paths.
Uses LocalSubprocessStore as destination — fresh empty store.
"""

from __future__ import annotations

import logging
import os
import shutil

import pytest

from conftest import NIX_BIN
from pynixd.store import LocalSocketStore, LocalSubprocessStore, SSHSubprocessStore, Store
from pynixd.operations.base import ByteCollector, PathInfo
from pynixd.wire import NixWriter
from pynixd.operations.store_mutations import (
    AddMultipleToStoreRequest,
    AddToStoreNarRequest,
)
from pynixd.connection import Connection

log = logging.getLogger(__name__)

DEST_STORE = "/tmp/pynixd-test-nar-dst"


@pytest.fixture
async def src_store() -> LocalSocketStore:
    """System store as source — has paths already."""
    s = LocalSocketStore(id="system")
    yield s
    await s.close()


@pytest.fixture
async def dst_store() -> LocalSubprocessStore:
    """Fresh empty store as destination."""
    shutil.rmtree(DEST_STORE, ignore_errors=True)
    os.makedirs(DEST_STORE, exist_ok=True)
    s = LocalSubprocessStore(store_path=DEST_STORE, id="dst", nix_bin=NIX_BIN)
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
    store: Store, path: str,
) -> tuple[PathInfo, bytes]:
    """Query PathInfo and fetch NAR for a path."""
    info = await store.query_path_info(path)
    assert info is not None, f"Path {path} not valid in store"
    nar = await store.buffer_nar_from_path(path)
    assert nar, f"Empty NAR for {path}"
    return info, nar


# ── AddToStoreNar ────────────────────────────────────────────────────


@pytest.mark.asyncio
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
        info.path, info.nar_size, info.nar_hash, info.references,
    )

    async with dst_store.build_conn() as dst:
        # Send via AddToStoreNar (raw data method on Connection)
        request = AddToStoreNarRequest(
            info=info, repair=0, dont_check_sigs=1,
        )
        await dst.add_to_store_nar(request, nar_data)

    # Verify it arrived
    valid_after = await dst_store.query_valid_paths({path})
    assert path in valid_after, "Path not in dest store after AddToStoreNar"

    # Verify content matches
    dst_info = await dst_store.query_path_info(path)
    assert dst_info is not None
    assert dst_info.nar_hash == info.nar_hash
    assert dst_info.nar_size == info.nar_size


# ── AddMultipleToStore ───────────────────────────────────────────────


@pytest.mark.asyncio
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
        info.path, info.nar_size, len(nar_data), info.references,
    )

    async with dst_store.build_conn() as dst:
        # Build the framed payload: count + (ValidPathInfo + raw NAR)
        payload = ByteCollector()
        w = NixWriter(payload)
        w.write_uint64(1)  # count
        w.write_string(info.path)
        w.write_string(info.deriver)
        w.write_string(info.nar_hash)
        w.write_string_set(info.references)
        w.write_uint64(info.registration_time)
        w.write_uint64(info.nar_size)
        w.write_uint64(info.ultimate)
        w.write_string_set(info.sigs)
        w.write_string(info.ca)
        w.write(nar_data)  # raw NAR, no length prefix

        await dst.add_multiple_to_store(
            AddMultipleToStoreRequest(repair=0, dont_check_sigs=1),
            payload.getvalue(),
        )

    # Verify it arrived
    valid_after = await dst_store.query_valid_paths({path})
    assert path in valid_after, "Path not in dest store after AddMultipleToStore"

    dst_info = await dst_store.query_path_info(path)
    assert dst_info is not None
    assert dst_info.nar_hash == info.nar_hash


@pytest.mark.asyncio
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

    assert len(picked) == 2, f"Could not find 2 small self-contained paths (found {len(picked)})"

    async with dst_store.build_conn() as dst:
        paths = {p for p, _, _ in picked}

        # Build payload with count=2
        payload = ByteCollector()
        w = NixWriter(payload)
        w.write_uint64(2)  # count

        for _path, info, nar_data in picked:
            w.write_string(info.path)
            w.write_string(info.deriver)
            w.write_string(info.nar_hash)
            w.write_string_set(info.references)
            w.write_uint64(info.registration_time)
            w.write_uint64(info.nar_size)
            w.write_uint64(info.ultimate)
            w.write_string_set(info.sigs)
            w.write_string(info.ca)
            w.write(nar_data)

        await dst.add_multiple_to_store(
            AddMultipleToStoreRequest(repair=0, dont_check_sigs=1),
            payload.getvalue(),
        )

    # Verify both arrived
    paths = {p for p, _, _ in picked}
    valid_after = await dst_store.query_valid_paths(paths)
    for p, _, _ in picked:
        assert p in valid_after, f"Path {p} not in dest after AddMultipleToStore"


# ── copy_paths (streaming AddMultipleToStore) ─────────────────────


COPY_DEST_STORE = "/tmp/pynixd-test-nar-copy"


@pytest.fixture
async def copy_dst_store() -> LocalSubprocessStore:
    """Separate dest store for copy_paths tests."""
    shutil.rmtree(COPY_DEST_STORE, ignore_errors=True)
    os.makedirs(COPY_DEST_STORE, exist_ok=True)
    s = LocalSubprocessStore(store_path=COPY_DEST_STORE, id="copy-dst", nix_bin=NIX_BIN)
    yield s
    await s.close()


@pytest.mark.asyncio
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
        info.path, info.nar_size, info.nar_hash,
    )

    await copy_dst_store.stream_paths_store_to_store(src_store, [(path, info)])

    # Verify it arrived
    valid_after = await copy_dst_store.query_valid_paths({path})
    assert path in valid_after, "Path not in dest after copy_paths"

    dst_info = await copy_dst_store.query_path_info(path)
    assert dst_info is not None
    assert dst_info.nar_hash == info.nar_hash
    assert dst_info.nar_size == info.nar_size


@pytest.mark.asyncio
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


STREAM_DEST_STORE = "/tmp/pynixd-test-nar-stream"


@pytest.fixture
async def stream_dst_store() -> LocalSubprocessStore:
    """Separate dest store for streaming tests."""
    shutil.rmtree(STREAM_DEST_STORE, ignore_errors=True)
    os.makedirs(STREAM_DEST_STORE, exist_ok=True)
    s = LocalSubprocessStore(store_path=STREAM_DEST_STORE, id="stream-dst", nix_bin=NIX_BIN)
    yield s
    await s.close()


@pytest.mark.asyncio
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
        info.path, info.nar_size, info.nar_hash,
    )

    await stream_dst_store.pipe_nar_from(src_store, path, info)

    # Verify it arrived
    valid_after = await stream_dst_store.query_valid_paths({path})
    assert path in valid_after, "Path not in dest after pipe_nar_from"

    dst_info = await stream_dst_store.query_path_info(path)
    assert dst_info is not None
    assert dst_info.nar_hash == info.nar_hash
    assert dst_info.nar_size == info.nar_size


@pytest.mark.asyncio
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
async def nixbuild_store() -> SSHSubprocessStore:
    """nixbuild.net store (protocol 1.32)."""
    username = os.environ.get("USER", "root")
    s = SSHSubprocessStore(
        host="eu.nixbuild.net",
        username=username,
        id="nixbuild",
        max_builds=2,
    )
    yield s
    await s.close()


@pytest.mark.nixbuild
@pytest.mark.asyncio
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
        info.path, info.nar_size,
    )

    await nixbuild_store.stream_paths_store_to_store(src_store, [(path, info)])

    # Verify it arrived
    valid = await nixbuild_store.query_valid_paths({path})
    assert path in valid, "Path not on nixbuild after copy_paths"

    nb_info = await nixbuild_store.query_path_info(path)
    assert nb_info is not None
    assert nb_info.nar_hash == info.nar_hash


@pytest.mark.nixbuild
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_copy_paths_roundtrip_nixbuild(
    src_store: LocalSocketStore,
    nixbuild_store: SSHSubprocessStore,
) -> None:
    """Push paths to nixbuild, then pull them back via copy_paths (proto 1.32)."""
    roundtrip_store = "/tmp/pynixd-test-nar-roundtrip"
    shutil.rmtree(roundtrip_store, ignore_errors=True)
    os.makedirs(roundtrip_store, exist_ok=True)
    roundtrip_store_instance = LocalSubprocessStore(
        store_path=roundtrip_store, id="roundtrip", nix_bin=NIX_BIN,
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
        log.info("Pushed %d paths to nixbuild", len(picked))

        # Pull back from nixbuild to fresh local store
        await roundtrip_store_instance.stream_paths_store_to_store(nixbuild_store, picked)
        log.info("Pulled %d paths from nixbuild", len(picked))

        # Verify all arrived with matching hashes
        for path, orig_info in picked:
            rt_info = await roundtrip_store_instance.query_path_info(path)
            assert rt_info is not None, f"{path} missing after roundtrip"
            assert rt_info.nar_hash == orig_info.nar_hash
            assert rt_info.nar_size == orig_info.nar_size
    finally:
        await roundtrip_store_instance.close()
