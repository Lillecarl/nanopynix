"""Content-Addressed (CA) derivation operation tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pyinstrument
import pytest
import structlog

from pynixd import Server
from pynixd.store import LocalSocketStore
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    rmtree_robust,
)

log = structlog.get_logger(__name__)

TEST_CA_NIX = Path("test-ca.nix")

CA_EXTRA_ARGS = [
    "--option",
    "extra-experimental-features",
    "ca-derivations",
]

CA_NIX_CONFIG = {
    "extra-experimental-features": "ca-derivations",
}


def _ca_test_store_kwargs(**overrides) -> dict:
    kwargs = get_test_store_kwargs(
        extra_args=CA_EXTRA_ARGS,
        extra_env=CA_NIX_CONFIG,
    )
    return kwargs | overrides


@pytest.fixture
async def ca_env(tmp_path: Path):
    """Set up a pynixd server with CA-derivations enabled."""
    async with asyncio.timeout(120):
        pynixd_local_path = STORE_PREFIX / "pynixd-local-ca"
        pynixd_builder_path = STORE_PREFIX / "pynixd-builder-ca"
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_builder_path)

        pynixd_local = LocalSocketStore(
            id="pynixd-local-ca",
            store_path=pynixd_local_path,
            **_ca_test_store_kwargs(),
        )
        pynixd_builder = LocalSocketStore(
            id="pynixd-builder-ca",
            store_path=pynixd_builder_path,
            **_ca_test_store_kwargs(),
        )

        async with Server(
            local_store=pynixd_local,
            stores={"builder": pynixd_builder},
            ssh_port=0,
        ) as server:
            username = os.environ.get("USER", "root")
            uri = f"ssh-ng://{username}@127.0.0.1:{server.port}"
            yield server, uri


async def test_ca_simple_build_root_store(
    profiler: pyinstrument.Profiler,
) -> None:
    """Build a CA floating derivation directly against the managed daemon (no pynixd)."""
    async with asyncio.timeout(120):
        store_path = STORE_PREFIX / "ca-root-test"
        rmtree_robust(store_path)

        store = LocalSocketStore(
            id="ca-root",
            store_path=store_path,
            **_ca_test_store_kwargs(),
        )

        try:
            await store.ensure_daemon()

            cmd = [
                str(NIX_BIN),
                "build",
                "--store",
                str(store_path),
                "--extra-experimental-features",
                "ca-derivations",
                "--file",
                str(TEST_CA_NIX),
                "ca_simple",
                "--no-link",
                "--print-out-paths",
            ]
            rc, stdout, stderr, stdboth = await run_subproc(
                cmd, nix_config=CA_NIX_CONFIG
            )
            assert rc == 0, f"CA simple build failed:\n{stdboth}"
            out_path = stdout.strip()
            assert out_path.startswith("/nix/store/"), (
                f"Unexpected output path: {out_path}"
            )
            log.info("ca_simple_output", path=out_path)
        finally:
            await store.close()


async def test_ca_multi_output_build_root_store(
    profiler: pyinstrument.Profiler,
) -> None:
    """Build a CA derivation with multiple outputs directly against the managed daemon."""
    async with asyncio.timeout(120):
        store_path = STORE_PREFIX / "ca-root-multi"
        rmtree_robust(store_path)

        store = LocalSocketStore(
            id="ca-root-multi",
            store_path=store_path,
            **_ca_test_store_kwargs(),
        )

        try:
            await store.ensure_daemon()

            cmd = [
                str(NIX_BIN),
                "build",
                "--store",
                str(store_path),
                "--extra-experimental-features",
                "ca-derivations",
                "--file",
                str(TEST_CA_NIX),
                "ca_multi_output",
                "--no-link",
                "--print-out-paths",
            ]
            rc, stdout, stderr, stdboth = await run_subproc(
                cmd, nix_config=CA_NIX_CONFIG
            )
            assert rc == 0, f"CA multi-output build failed:\n{stdboth}"
            paths = stdout.strip().splitlines()
            assert len(paths) == 2, (
                f"Expected 2 output paths, got {len(paths)}: {paths}"
            )
            for p in paths:
                assert p.startswith("/nix/store/"), f"Unexpected output path: {p}"
            log.info("ca_multi_output_paths", paths=paths)
        finally:
            await store.close()


async def test_ca_depends_on_ca_root_store(
    profiler: pyinstrument.Profiler,
) -> None:
    """Build a CA derivation that depends on another CA derivation."""
    async with asyncio.timeout(120):
        store_path = STORE_PREFIX / "ca-root-depends"
        rmtree_robust(store_path)

        store = LocalSocketStore(
            id="ca-root-depends",
            store_path=store_path,
            **_ca_test_store_kwargs(),
        )

        try:
            await store.ensure_daemon()

            cmd = [
                str(NIX_BIN),
                "build",
                "--store",
                str(store_path),
                "--extra-experimental-features",
                "ca-derivations",
                "--file",
                str(TEST_CA_NIX),
                "ca_depends_on_ca",
                "--no-link",
                "--print-out-paths",
            ]
            rc, stdout, stderr, stdboth = await run_subproc(
                cmd, nix_config=CA_NIX_CONFIG
            )
            assert rc == 0, f"CA depends-on-CA build failed:\n{stdboth}"
            out_path = stdout.strip()
            assert out_path.startswith("/nix/store/"), (
                f"Unexpected output path: {out_path}"
            )
            log.info("ca_depends_on_ca_output", path=out_path)
        finally:
            await store.close()


async def test_non_ca_depends_on_ca_root_store(
    profiler: pyinstrument.Profiler,
) -> None:
    """Build a non-CA (deferred) derivation that depends on a CA derivation."""
    async with asyncio.timeout(120):
        store_path = STORE_PREFIX / "ca-root-non-ca-depends"
        rmtree_robust(store_path)

        store = LocalSocketStore(
            id="ca-root-non-ca-depends",
            store_path=store_path,
            **_ca_test_store_kwargs(),
        )

        try:
            await store.ensure_daemon()

            cmd = [
                str(NIX_BIN),
                "build",
                "--store",
                str(store_path),
                "--extra-experimental-features",
                "ca-derivations",
                "--file",
                str(TEST_CA_NIX),
                "non_ca_depends_on_ca",
                "--no-link",
                "--print-out-paths",
            ]
            rc, stdout, stderr, stdboth = await run_subproc(
                cmd, nix_config=CA_NIX_CONFIG
            )
            assert rc == 0, f"Non-CA depends-on-CA build failed:\n{stdboth}"
            out_path = stdout.strip()
            assert out_path.startswith("/nix/store/"), (
                f"Unexpected output path: {out_path}"
            )
            log.info("non_ca_depends_on_ca_output", path=out_path)
        finally:
            await store.close()


async def test_ca_simple_via_pynixd(profiler: pyinstrument.Profiler, ca_env) -> None:
    """Build a CA floating derivation through pynixd."""
    async with asyncio.timeout(120):
        server, uri = ca_env

        cmd = [
            str(NIX_BIN),
            "build",
            "--eval-store",
            "auto",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations",
            "--file",
            str(TEST_CA_NIX),
            "ca_simple",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(
            cmd, nix_config=CA_NIX_CONFIG, expected_retcode=None
        )
        log.info("ca_simple_via_pynixd", rc=rc, stdout=stdout, stderr=stderr)
        assert rc == 0, f"CA simple build via pynixd failed:\n{stdboth}"


async def test_ca_query_derivation_output_map_root_store(
    profiler: pyinstrument.Profiler,
) -> None:
    """Build CA derivation then verify its output map via nix path-info."""
    async with asyncio.timeout(120):
        store_path = STORE_PREFIX / "ca-root-qdom"
        rmtree_robust(store_path)

        store = LocalSocketStore(
            id="ca-root-qdom",
            store_path=store_path,
            **_ca_test_store_kwargs(),
        )

        try:
            await store.ensure_daemon()

            # Build the CA derivation
            cmd = [
                str(NIX_BIN),
                "build",
                "--store",
                str(store_path),
                "--extra-experimental-features",
                "ca-derivations",
                "--file",
                str(TEST_CA_NIX),
                "ca_simple",
                "--no-link",
                "--print-out-paths",
            ]
            rc, stdout, stderr, stdboth = await run_subproc(
                cmd, nix_config=CA_NIX_CONFIG
            )
            assert rc == 0, f"CA build failed:\n{stdboth}"
            out_path = stdout.strip()
            assert out_path.startswith("/nix/store/")

            # Get the .drv path
            cmd = [
                str(NIX_BIN),
                "eval",
                "--store",
                str(store_path),
                "--extra-experimental-features",
                "ca-derivations",
                "--file",
                str(TEST_CA_NIX),
                "ca_simple.drvPath",
                "--raw",
            ]
            rc, drv_out, stderr, stdboth = await run_subproc(
                cmd, nix_config=CA_NIX_CONFIG
            )
            assert rc == 0, f"CA drvPath eval failed:\n{stdboth}"
            drv_path = drv_out.strip()

            # Query the CA derivation's output map via nix path-info.
            # This exercises QueryDerivationOutputMap (op 41) and RegisterDrvOutput (op 42).
            cmd = [
                str(NIX_BIN),
                "path-info",
                "--store",
                str(store_path),
                "--extra-experimental-features",
                "ca-derivations",
                "--json",
                f"{drv_path}^*",
            ]
            rc, stdout, stderr, stdboth = await run_subproc(
                cmd, nix_config=CA_NIX_CONFIG
            )
            assert rc == 0, f"path-info failed:\n{stderr}"
            import json

            info = json.loads(stdout)
            assert out_path in info, f"Expected {out_path} in {info}"
            # CA derivations have a "ca" field in their output info
            assert "ca" in info[out_path], f"Expected 'ca' field in {info[out_path]}"

        finally:
            await store.close()
