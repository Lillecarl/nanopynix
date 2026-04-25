"""Functional tests for HTTP binary cache upload (PUT)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import pytest
import structlog

from pynixd.operations.nar_from_path import NarFromPathRequest
from pynixd.operations.query_path_info import QueryPathInfoRequest
from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from tests.conftest import (
    NIX_BIN,
    SESSION_HTTP_PASS,
    SESSION_HTTP_USER,
    get_test_store_kwargs,
    run_subproc,
)

if TYPE_CHECKING:
    from pynixd import Server

log = structlog.get_logger(__name__)

HTTP_AUTH_HEADER = "Basic " + base64.b64encode(f"{SESSION_HTTP_USER}:{SESSION_HTTP_PASS}".encode()).decode()


async def get_hello_path() -> StorePath:
    """Build nixpkgs#hello and return its store path."""
    rc, stdout, stderr, _ = await run_subproc(
        [str(NIX_BIN), "path-info", "nixpkgs#hello"],
    )
    if rc != 0:
        await run_subproc([str(NIX_BIN), "build", "nixpkgs#hello"])
        rc, stdout, stderr, _ = await run_subproc(
            [str(NIX_BIN), "path-info", "nixpkgs#hello"],
        )
    return StorePath(stdout.strip())


async def get_no_refs_path() -> StorePath:
    """Create a path with no references and return its store path."""
    rc, stdout, stderr, _ = await run_subproc(
        [
            str(NIX_BIN),
            "build",
            "--no-link",
            "--print-out-paths",
            "--impure",
            "--expr",
            'builtins.toFile "no-refs-test" "some random content"',
        ],
    )
    assert rc == 0, f"Failed to create no-refs path: {stderr}"
    return StorePath(stdout.strip())


@pytest.mark.timeout(60)
async def test_http_upload(
    pynixd_server: Server,
) -> None:
    """Test uploading a path to the HTTP cache via PUT using aiohttp directly.

    Store operations triggered:
    - None: This test only checks HTTP upload functionality without triggering explicit Store operations
    """
    # Use no-refs path for direct PUT to avoid dependency issues
    path = await get_no_refs_path()
    hash_part = path.hash_part()

    # Get its NAR and narinfo from root store
    root_store = LocalSocketStore(
        id="root",
        store_path=Path("/"),
        **get_test_store_kwargs(no_probe=True),
    )
    info_resp = await root_store.execute(QueryPathInfoRequest(path=path))
    assert info_resp.valid and info_resp.info
    vinfo = info_resp.info.with_path(path)

    nar_data = bytearray()

    async def collect_nar(chunk: bytes):
        nar_data.extend(chunk)

    await root_store.execute(
        NarFromPathRequest(
            path=path,
            nar_size=vinfo.nar_size,
            async_callback=collect_nar,
        ),
    )

    narinfo = vinfo.to_narinfo()

    base_url = f"http://127.0.0.1:{pynixd_server.http_bound_port}"

    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": HTTP_AUTH_HEADER}
        # 3. PUT NAR
        nar_hash_part = vinfo.nar_hash.split(":")[-1]
        log.info("uploading_nar", hash=nar_hash_part, size=len(nar_data))
        async with session.put(
            f"{base_url}/nar/{nar_hash_part}.nar",
            data=nar_data,
            headers=headers,
        ) as resp:
            assert resp.status == 200
            assert await resp.text() == "ok\n"

        # 4. PUT .narinfo
        log.info("uploading_narinfo", hash=hash_part)
        async with session.put(
            f"{base_url}/{hash_part}.narinfo",
            data=narinfo,
            headers=headers,
        ) as resp:
            assert resp.status == 200
            assert await resp.text() == "ok\n"

    # 5. Verify it now exists in the local store (HTTP uploads go to local_store)
    info_resp = await pynixd_server.local_store.execute(QueryPathInfoRequest(path=path))
    assert info_resp.valid, f"Path {path} should be valid in local store after upload"


@pytest.mark.timeout(60)
async def test_nix_copy_to_http(
    pynixd_server: Server,
) -> None:
    """Test copying a path to the HTTP cache using 'nix copy --to http://...'.

    Store operations triggered:
    - None: This test only checks HTTP upload functionality via nix copy without triggering explicit Store operations
    """
    # Use hello as requested by user - nix copy handles the closure
    path = await get_hello_path()

    # 3. Use 'nix copy' to upload
    # Nix copy expects URL with embedded credentials for basic auth
    auth_url = f"http://{SESSION_HTTP_USER}:{SESSION_HTTP_PASS}@127.0.0.1:{pynixd_server.http_bound_port}"
    cmd = [
        str(NIX_BIN),
        "copy",
        "--to",
        auth_url,
        str(path),
    ]
    rc, stdout, stderr, _ = await run_subproc(cmd)
    assert rc == 0, f"nix copy failed:\n{stderr}"

    # 4. Verify it now exists in the local store (HTTP uploads go to local_store)
    info_resp = await pynixd_server.local_store.execute(QueryPathInfoRequest(path=path))
    assert info_resp.valid, f"Path {path} should be valid in local store after nix copy"
