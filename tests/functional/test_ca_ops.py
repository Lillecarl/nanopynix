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
    """Build a CA floating derivation through pynixd.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryValidPaths: Queries valid paths
    """
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


async def test_ca_multi_output_via_pynixd(
    profiler: pyinstrument.Profiler, ca_env
) -> None:
    """Build a CA derivation with multiple outputs through pynixd.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryValidPaths: Queries valid paths
    """
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
            "ca_multi_output",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(
            cmd, nix_config=CA_NIX_CONFIG, expected_retcode=None
        )
        log.info("ca_multi_output_via_pynixd", rc=rc, stdout=stdout, stderr=stderr)
        assert rc == 0, f"CA multi-output build via pynixd failed:\n{stdboth}"
        paths = stdout.strip().splitlines()
        assert len(paths) == 2, f"Expected 2 outputs, got {len(paths)}: {paths}"


async def test_ca_depends_on_ca_via_pynixd(
    profiler: pyinstrument.Profiler, ca_env
) -> None:
    """Build a CA derivation that depends on another CA derivation through pynixd.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryValidPaths: Queries valid paths
    """
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
            "ca_depends_on_ca",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(
            cmd, nix_config=CA_NIX_CONFIG, expected_retcode=None
        )
        log.info("ca_depends_on_ca_via_pynixd", rc=rc, stdout=stdout, stderr=stderr)
        assert rc == 0, f"CA depends-on-CA build via pynixd failed:\n{stdboth}"
        assert stdout.strip().startswith("/nix/store/"), f"Unexpected output: {stdout}"


async def test_non_ca_depends_on_ca_via_pynixd(
    profiler: pyinstrument.Profiler, ca_env
) -> None:
    """Build a deferred (non-CA) derivation that depends on a CA derivation through pynixd.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryValidPaths: Queries valid paths
    """
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
            "non_ca_depends_on_ca",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(
            cmd, nix_config=CA_NIX_CONFIG, expected_retcode=None
        )
        log.info("non_ca_depends_on_ca_via_pynixd", rc=rc, stdout=stdout, stderr=stderr)
        assert rc == 0, f"Non-CA depends-on-CA build via pynixd failed:\n{stdboth}"
        assert stdout.strip().startswith("/nix/store/"), f"Unexpected output: {stdout}"


async def test_ca_query_derivation_output_map_via_pynixd(
    profiler: pyinstrument.Profiler, ca_env
) -> None:
    """Build CA derivation through pynixd then query its output map.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - AddTempRoot: Adds temporary root
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - IsValidPath: Checks if path exists
    - NarFromPath: Gets NAR from path
    - QueryDerivationOutputMap: Queries derivation output map
    - QueryMissing: Queries missing paths
    - QueryPathInfo: Queries path info
    - QueryValidPaths: Queries valid paths
    """
    async with asyncio.timeout(120):
        server, uri = ca_env

        # Build the CA derivation first
        build_cmd = [
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
            build_cmd, nix_config=CA_NIX_CONFIG, expected_retcode=None
        )
        assert rc == 0, f"CA build via pynixd failed:\n{stdboth}"

        # Get the .drv path
        eval_cmd = [
            str(NIX_BIN),
            "eval",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations",
            "--file",
            str(TEST_CA_NIX),
            "ca_simple.drvPath",
            "--raw",
        ]
        rc, drv_out, _, _ = await run_subproc(eval_cmd, nix_config=CA_NIX_CONFIG)
        assert rc == 0, f"CA drvPath eval failed:\n{_}"
        drv_path = drv_out.strip()

        # Query the output map — exercises QueryDerivationOutputMap (op 41)
        info_cmd = [
            str(NIX_BIN),
            "path-info",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations",
            "--json",
            f"{drv_path}^*",
        ]
        rc, info_out, _, _ = await run_subproc(info_cmd, nix_config=CA_NIX_CONFIG)
        assert rc == 0, f"path-info failed:\n{_}"
        import json

        info = json.loads(info_out)
        assert len(info) > 0, f"No output paths found for {drv_path}"
        for path, data in info.items():
            assert "ca" in data, f"Expected 'ca' field in {data}"


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
        rc, stdout, stderr, stdboth = await run_subproc(cmd, nix_config=CA_NIX_CONFIG)
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
        rc, drv_out, stderr, stdboth = await run_subproc(cmd, nix_config=CA_NIX_CONFIG)
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
        rc, stdout, stderr, stdboth = await run_subproc(cmd, nix_config=CA_NIX_CONFIG)
        assert rc == 0, f"path-info failed:\n{stderr}"
        import json

        info = json.loads(stdout)
        assert out_path in info, f"Expected {out_path} in {info}"
        assert "ca" in info[out_path], f"Expected 'ca' field in {info[out_path]}"


async def test_dynamic_drv_trampoline(profiler: pyinstrument.Profiler, dyn_env) -> None:
    """Build a nested dynamic derivation (producingDrv^out^out) through pynixd.

    This exercises the trampoline: producingDrv's output is hello.drv,
    and the scheduler should detect this and automatically build the
    inner hello derivation. The final output should be hello's store
    path, not the intermediate .drv.

    Store operations triggered:
    - AddTempRoot: Adds temporary root
    - AddToStore: Adds to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - IsValidPath: Checks if path exists
    - NarFromPath: Gets NAR from path
    - QueryDerivationOutputMap: Queries derivation output map
    - QueryMissing: Queries missing paths
    - QueryPathInfo: Queries path info
    """
    async with asyncio.timeout(120):
        server, uri = dyn_env

        # First, get producingDrv's .drv path
        eval_cmd = [
            str(NIX_BIN),
            "eval",
            "--option",
            "builders",
            "",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations dynamic-derivations",
            "--impure",
            "--file",
            str(DYN_NIX),
            "producingDrv.drvPath",
            "--raw",
        ]
        rc, drv_out, _, _ = await run_subproc(eval_cmd, nix_config=DYN_NIX_CONFIG)
        assert rc == 0, "drvPath eval failed"
        drv_path = drv_out.strip()

        # Build producingDrv^out^out (nested: the out output of the
        # out output's .drv)
        build_cmd = [
            str(NIX_BIN),
            "build",
            "--option",
            "builders",
            "",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations dynamic-derivations",
            f"{drv_path}^out^out",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(
            build_cmd, nix_config=DYN_NIX_CONFIG, expected_retcode=None
        )
        assert rc == 0, f"Dynamic trampoline build failed:\n{stdboth}"
        out_path = stdout.strip()
        assert out_path.startswith("/nix/store/"), f"Unexpected output: {out_path}"
        assert not out_path.endswith(".drv"), (
            f"Expected non-.drv output from trampoline, got: {out_path}"
        )


DYN_NIX = Path("test-dyn-drv.nix")

DYN_EXTRA_ARGS = [
    "--option",
    "extra-experimental-features",
    "ca-derivations dynamic-derivations",
]

DYN_NIX_CONFIG = {
    "extra-experimental-features": "ca-derivations dynamic-derivations",
}


@pytest.fixture
async def dyn_env(tmp_path: Path):
    """Set up a pynixd server with dynamic-derivations enabled.

    Does NOT use the root store as a substituter so builds actually
    go through pynixd's scheduler instead of being substituted.
    """
    async with asyncio.timeout(120):
        pynixd_local_path = STORE_PREFIX / "pynixd-local-dyn"
        pynixd_builder_path = STORE_PREFIX / "pynixd-builder-dyn"
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_builder_path)

        dyn_kwargs = get_test_store_kwargs(
            extra_args=DYN_EXTRA_ARGS,
            extra_env={
                "NIX_CONFIG": "extra-experimental-features = ca-derivations dynamic-derivations",
            },
        )

        pynixd_local = LocalSocketStore(
            id="pynixd-local-dyn",
            store_path=pynixd_local_path,
            **dyn_kwargs,
        )
        pynixd_builder = LocalSocketStore(
            id="pynixd-builder-dyn",
            store_path=pynixd_builder_path,
            **dyn_kwargs,
        )

        async with Server(
            local_store=pynixd_local,
            stores={"builder": pynixd_builder},
            ssh_port=0,
        ) as server:
            username = os.environ.get("USER", "root")
            uri = f"ssh-ng://{username}@127.0.0.1:{server.port}"
            yield server, uri


async def test_dynamic_drv_producing_via_pynixd(
    profiler: pyinstrument.Profiler, dyn_env
) -> None:
    """Build producingDrv (text-hashed CA whose output IS a .drv) through pynixd.

    producingDrv is a text-hashed CA derivation: its outputHashMode is "text"
    and the output content IS a .drv file. This tests that pynixd correctly
    handles the full lifecycle: build, realisation registration, and
    QueryDerivationOutputMap for text-hashed CA derivations.

    Store operations triggered:
    - AddTempRoot: Adds temporary root
    - AddToStore: Adds to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - IsValidPath: Checks if path exists
    - NarFromPath: Gets NAR from path
    - QueryDerivationOutputMap: Queries derivation output map
    - QueryMissing: Queries missing paths
    - QueryPathInfo: Queries path info
    """
    async with asyncio.timeout(120):
        server, uri = dyn_env

        # Build producingDrv + hello together (both needed for producingDrv's input_srcs)
        build_cmd = [
            str(NIX_BIN),
            "build",
            "--option",
            "builders",
            "",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations dynamic-derivations",
            "--impure",
            "--file",
            str(DYN_NIX),
            "producingDrv",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(
            build_cmd, nix_config=DYN_NIX_CONFIG, expected_retcode=None
        )
        assert rc == 0, f"producingDrv build via pynixd failed:\n{stdboth}"
        producing_out = stdout.strip()
        assert producing_out.startswith("/nix/store/"), (
            f"Unexpected output path: {producing_out}"
        )

        # The output of producingDrv IS a .drv file
        assert producing_out.endswith(".drv"), (
            f"Expected .drv output, got: {producing_out}"
        )

        # Verify it's parseable as a derivation
        pynixd_local_path = STORE_PREFIX / "pynixd-local-dyn"
        full_path = pynixd_local_path / producing_out.lstrip("/")
        if full_path.exists():
            content = full_path.read_text()
            assert content.startswith("Derive("), (
                f"Output should be derivation ATerm, got: {content[:80]}"
            )

        # Query the derivation output map to verify realisation registration
        eval_cmd = [
            str(NIX_BIN),
            "eval",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations dynamic-derivations",
            "--impure",
            "--file",
            str(DYN_NIX),
            "producingDrv.drvPath",
            "--raw",
        ]
        rc, drv_out, _, _ = await run_subproc(eval_cmd, nix_config=DYN_NIX_CONFIG)
        assert rc == 0, "producingDrv drvPath eval failed"
        drv_path = drv_out.strip()

        info_cmd = [
            str(NIX_BIN),
            "path-info",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations dynamic-derivations",
            "--json",
            f"{drv_path}^*",
        ]
        rc, info_out, _, _ = await run_subproc(info_cmd, nix_config=DYN_NIX_CONFIG)
        assert rc == 0, "path-info for producingDrv failed"
        import json

        info = json.loads(info_out)
        assert producing_out in info, (
            f"Expected {producing_out} in output map, got: {list(info.keys())}"
        )

        log.info("producingDrv_via_pynixd", output=producing_out)


async def test_text_hashed_ca_build_root_store(
    profiler: pyinstrument.Profiler,
) -> None:
    """Build a text-hashed CA derivation (outputHashMode=text) directly."""
    async with asyncio.timeout(120):
        store_path = STORE_PREFIX / "ca-root-text"
        rmtree_robust(store_path)

        store = LocalSocketStore(
            id="ca-root-text",
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
                "ca-derivations dynamic-derivations",
                "--file",
                str(TEST_CA_NIX),
                "ca_text_hashed",
                "--no-link",
                "--print-out-paths",
            ]
            rc, stdout, stderr, stdboth = await run_subproc(
                cmd, nix_config=CA_NIX_CONFIG
            )
            assert rc == 0, f"Text-hashed CA build failed:\n{stdboth}"
            out_path = stdout.strip()
            assert out_path.startswith("/nix/store/"), (
                f"Unexpected output path: {out_path}"
            )
            log.info("text_hashed_ca_output", path=out_path)

            full_path = store_path / out_path.lstrip("/")
            assert full_path.exists(), f"Output file missing: {full_path}"
            content = full_path.read_text().strip()
            assert content == "text-content", f"Unexpected content: {content!r}"
        finally:
            await store.close()


async def test_text_hashed_ca_build_via_pynixd(
    profiler: pyinstrument.Profiler, ca_env
) -> None:
    """Build a text-hashed CA derivation through pynixd proxy.

    Store operations triggered:
    - AddTempRoot: Adds temporary root
    - AddToStore: Adds to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - IsValidPath: Checks if path exists
    - NarFromPath: Gets NAR from path
    - QueryDerivationOutputMap: Queries derivation output map
    - QueryMissing: Queries missing paths
    - QueryPathInfo: Queries path info
    """
    async with asyncio.timeout(120):
        server, uri = ca_env

        build_cmd = [
            str(NIX_BIN),
            "build",
            "--option",
            "builders",
            "",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations dynamic-derivations",
            "--file",
            str(TEST_CA_NIX),
            "ca_text_hashed",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(
            build_cmd, nix_config=CA_NIX_CONFIG, expected_retcode=None
        )
        assert rc == 0, f"Text-hashed CA build via pynixd failed:\n{stdboth}"
        out_path = stdout.strip()
        assert out_path.startswith("/nix/store/"), f"Unexpected output path: {out_path}"
        log.info("text_hashed_ca_via_pynixd", path=out_path)

        # Get the .drv path
        eval_cmd = [
            str(NIX_BIN),
            "eval",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations dynamic-derivations",
            "--file",
            str(TEST_CA_NIX),
            "ca_text_hashed.drvPath",
            "--raw",
        ]
        rc, drv_out, _, _ = await run_subproc(eval_cmd, nix_config=CA_NIX_CONFIG)
        assert rc == 0, "CA drvPath eval failed"
        drv_path = drv_out.strip()

        # Query the output map — exercises QueryDerivationOutputMap for text-hashed CA
        info_cmd = [
            str(NIX_BIN),
            "path-info",
            "--store",
            uri,
            "--extra-experimental-features",
            "ca-derivations dynamic-derivations",
            "--json",
            f"{drv_path}^*",
        ]
        rc, info_out, _, _ = await run_subproc(info_cmd, nix_config=CA_NIX_CONFIG)
        assert rc == 0, "path-info failed"
        import json

        info = json.loads(info_out)
        assert len(info) > 0, f"No output paths found for {drv_path}"
        assert out_path in info, f"Expected {out_path} in {info}"
        assert "ca" in info[out_path], f"Expected 'ca' field in {info[out_path]}"
