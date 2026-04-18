"""Standalone test for deferred CA derivation resolution via store protocol.

Exercises the exact code path that pynixd's scheduler will use:
1. Build reference paths on a root store via nix CLI
2. Create a clean builder store
3. Transfer needed paths via stream_paths_store_to_store
4. Register CA realisation via RegisterDrvOutputRequest
5. Resolve the deferred derivation via resolve_derivation()
6. Place the resolved .drv on both stores:
   - Local store: overwrite the .drv file directly (we own the filesystem)
   - Builder store: AddToStoreNar with updated ValidPathInfo + NAR of resolved content
7. BuildDerivation with the original .drv path and resolved BasicDerivation
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from pynixd.drv_parser import read_drv_file, to_basic_derivation
from pynixd.derivation_resolution import resolve_derivation, _unparse_basic_derivation
from pynixd.operations.base import BuildMode, ValidPathInfo, UnkeyedValidPathInfo
from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.operations.ca_derivations import RegisterDrvOutputRequest
from pynixd.operations.query_valid_paths import QueryValidPathsRequest
from pynixd.operations.query_derivation_output_map import (
    QueryDerivationOutputMapRequest,
)
from pynixd.operations.query_derivation_outputs_batch import (
    QueryDerivationOutputsBatchRequest,
)
from pynixd.operations.add_to_store_nar import AddToStoreNarRequest
from pynixd.operations.query_path_info import QueryPathInfoRequest
from pynixd.wire import FramedWriter
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    rmtree_robust,
)

CA_NIX = Path(__file__).resolve().parent.parent.parent / "test-ca.nix"
CA_EXTRA_ARGS = ["--option", "extra-experimental-features", "ca-derivations"]
CA_NIX_CONFIG = {"extra-experimental-features": "ca-derivations"}


def make_nar_regular_file(content: bytes) -> bytes:
    """Create a NAR archive for a single regular file with the given content."""
    parts = []
    parts.append(b"(\n")
    parts.append(b"type")
    parts.append(b"regular")
    if b"\x00" in content:
        parts.append(b"executable")
        parts.append(b"")
    parts.append(b"contents")
    parts.append(len(content).to_bytes(8, "little"))
    parts.append(content)
    pad = 8 - (len(content) % 8)
    if pad < 8:
        parts.append(b"\x00" * pad)
    parts.append(b")")
    return b"".join(parts)


def nar_hash_and_size(content: bytes) -> tuple[str, int]:
    """Compute SHA-256 hash and size of a NAR for a regular file with given content."""
    nar = make_nar_regular_file(content)
    h = hashlib.sha256(nar).hexdigest()
    return f"sha256:{h}", len(nar)


async def add_resolved_drv_via_nar(
    store: LocalSocketStore,
    drv_path: StorePath,
    resolved_aterm: bytes,
) -> bool:
    """Overwrite a .drv file's content on a store via AddToStoreNar.

    This replaces the existing file content at drv_path with the resolved ATerm.
    The daemon accepts AddToStoreNar for paths it already knows about (with repair=1).
    """
    nar_hash, nar_size = nar_hash_and_size(resolved_aterm)

    info = ValidPathInfo(
        path=drv_path,
        deriver=StorePath(""),
        nar_hash=nar_hash,
        references=set(),
        registration_time=0,
        nar_size=nar_size,
        ultimate=0,
        sigs=set(),
        ca="",
    )

    async def provide_nar(writer):
        fw = writer.framed()
        nar = make_nar_regular_file(resolved_aterm)
        fw.write(nar)
        await fw.finalize()

    req = AddToStoreNarRequest(
        info=info,
        repair=1,
        dont_check_sigs=1,
        async_provider=provide_nar,
    )
    try:
        resp = await req.execute(store, suppress_last=True)
        print(f"  AddToStoreNar succeeded for {drv_path}")
        return True
    except Exception as e:
        print(f"  AddToStoreNar FAILED: {type(e).__name__}: {e}")
        return False


async def main() -> None:
    # -- Step 1: Build reference on root store --
    print("=" * 70)
    print("Step 1: Build reference on root store")
    print("=" * 70)

    root_path = STORE_PREFIX / "deferred-replay-root"
    rmtree_robust(root_path)
    root_kwargs = get_test_store_kwargs(
        extra_args=CA_EXTRA_ARGS, extra_env=CA_NIX_CONFIG
    )
    root_store = LocalSocketStore(
        id="deferred-replay-root", store_path=root_path, **root_kwargs
    )
    await root_store.ensure_daemon()

    cmd_ca = [
        NIX_BIN,
        "build",
        "--store",
        str(root_path),
        "--extra-experimental-features",
        "ca-derivations",
        "--file",
        str(CA_NIX),
        "ca_simple",
        "--no-link",
        "--print-out-paths",
    ]
    rc, stdout, stderr, _ = await run_subproc(cmd_ca, nix_config=CA_NIX_CONFIG)
    assert rc == 0, f"CA build failed: {stderr}"
    ca_out_path = stdout.strip()
    print(f"CA output: {ca_out_path}")

    cmd_deferred = [
        NIX_BIN,
        "build",
        "--store",
        str(root_path),
        "--extra-experimental-features",
        "ca-derivations",
        "--file",
        str(CA_NIX),
        "non_ca_depends_on_ca",
        "--no-link",
        "--print-out-paths",
    ]
    rc, stdout, stderr, _ = await run_subproc(cmd_deferred, nix_config=CA_NIX_CONFIG)
    assert rc == 0, f"Deferred build failed: {stderr}"
    deferred_out_path = stdout.strip()
    print(f"Deferred output: {deferred_out_path}")

    # -- Step 2: Parse .drv files and get realisation --
    print()
    print("=" * 70)
    print("Step 2: Parse .drv files and get CA realisation")
    print("=" * 70)

    eval_cmd = [
        NIX_BIN,
        "eval",
        "--store",
        str(root_path),
        "--extra-experimental-features",
        "ca-derivations",
        "--file",
        str(CA_NIX),
        "ca_simple.drvPath",
        "--raw",
    ]
    rc, stdout, _, _ = await run_subproc(eval_cmd, nix_config=CA_NIX_CONFIG)
    ca_drv_path = StorePath(stdout.strip())
    print(f"CA .drv path: {ca_drv_path}")

    eval_cmd2 = [
        NIX_BIN,
        "eval",
        "--store",
        str(root_path),
        "--extra-experimental-features",
        "ca-derivations",
        "--file",
        str(CA_NIX),
        "non_ca_depends_on_ca.drvPath",
        "--raw",
    ]
    rc, stdout, _, _ = await run_subproc(eval_cmd2, nix_config=CA_NIX_CONFIG)
    deferred_drv_path = StorePath(stdout.strip())
    print(f"Deferred .drv path: {deferred_drv_path}")

    ca_parsed = read_drv_file(root_store.store_path, ca_drv_path)
    deferred_parsed = read_drv_file(root_store.store_path, deferred_drv_path)

    for o in deferred_parsed.outputs:
        print(f"  output: name={o.name} path={o.path!r} hash_algo={o.hash_algo!r}")

    # Get CA realisation via nix CLI
    realisation_cmd = [
        NIX_BIN,
        "realisation",
        "info",
        "--store",
        str(root_path),
        "--extra-experimental-features",
        "ca-derivations",
        "--json",
        f"{ca_drv_path}^out",
    ]
    rc, realisation_out, _, _ = await run_subproc(
        realisation_cmd, nix_config=CA_NIX_CONFIG
    )
    realisations_raw = (
        json.loads(realisation_out) if rc == 0 and realisation_out.strip() else []
    )
    realisation_to_register = realisations_raw[0] if realisations_raw else None
    print(f"CA realisation: {realisation_to_register}")

    # -- Step 3: Create builder store and transfer paths --
    print()
    print("=" * 70)
    print("Step 3: Create builder store and transfer paths")
    print("=" * 70)

    builder_path = STORE_PREFIX / "deferred-replay-builder"
    rmtree_robust(builder_path)
    builder_kwargs = get_test_store_kwargs(
        extra_args=CA_EXTRA_ARGS, extra_env=CA_NIX_CONFIG
    )
    builder_store = LocalSocketStore(
        id="deferred-replay-builder", store_path=builder_path, **builder_kwargs
    )
    await builder_store.ensure_daemon()

    paths_to_transfer: set[StorePath] = set()
    paths_to_transfer.add(ca_drv_path)
    paths_to_transfer.update(ca_parsed.input_srcs)
    paths_to_transfer.add(StorePath(ca_out_path).with_store_prefix())
    paths_to_transfer.add(deferred_drv_path)
    paths_to_transfer.update(deferred_parsed.input_srcs)
    for input_drv in deferred_parsed.input_drvs:
        paths_to_transfer.add(input_drv)

    print(f"Transferring {len(paths_to_transfer)} paths...")
    await LocalSocketStore.stream_paths_store_to_store(
        root_store, builder_store, paths_to_transfer
    )

    valid_resp = await builder_store.execute(
        QueryValidPathsRequest(paths=paths_to_transfer, substitute=0)
    )
    print(
        f"Builder store has {len(valid_resp.paths)} of {len(paths_to_transfer)} paths"
    )

    # -- Step 4: Register CA realisation on builder --
    print()
    print("=" * 70)
    print("Step 4: Register CA realisation on builder store")
    print("=" * 70)

    if realisation_to_register:
        try:
            reg_req = RegisterDrvOutputRequest(realisation=realisation_to_register)
            await builder_store.call(reg_req, suppress_last=True)
            print("CA realisation registered on builder store!")
        except Exception as e:
            print(f"Registration FAILED: {e}")

    # -- Step 4b: Resolve and replace BEFORE any BuildDerivation --
    # This must happen BEFORE the builder daemon has a chance to cache
    # the unresolved .drv content.
    print()
    print("=" * 70)
    print("Step 4b: Early resolve and replace .drv content")
    print("=" * 70)

    resolved_output_paths_early: dict[str, StorePath] = {
        "out": StorePath(ca_out_path).with_store_prefix()
    }
    resolved_early = resolve_derivation(
        deferred_parsed, deferred_drv_path, resolved_output_paths_early
    )
    resolved_aterm_early = _unparse_basic_derivation(resolved_early, mask_outputs=False)

    # Local store: overwrite filesystem
    drv_rel = str(deferred_drv_path).lstrip("/")
    local_fs_path = root_store.store_path / drv_rel
    if local_fs_path.exists():
        local_fs_path.chmod(0o644)
    with open(local_fs_path, "w") as f:
        f.write(resolved_aterm_early)
    print(
        f"Wrote resolved .drv to local store filesystem ({len(resolved_aterm_early)} chars)"
    )

    # Builder store: overwrite filesystem
    builder_fs_path = builder_store.store_path / drv_rel
    if builder_fs_path.exists():
        builder_fs_path.chmod(0o644)
    with open(builder_fs_path, "w") as f:
        f.write(resolved_aterm_early)
    print(
        f"Wrote resolved .drv to builder store filesystem ({len(resolved_aterm_early)} chars)"
    )

    # Restart builder daemon to clear any cached derivation parse
    print("Restarting builder daemon to clear caches...")
    await builder_store.close()
    builder_store = LocalSocketStore(
        id="deferred-replay-builder", store_path=builder_path, **builder_kwargs
    )
    await builder_store.ensure_daemon()
    print("Builder daemon restarted.")

    # Re-register CA realisation after daemon restart
    if realisation_to_register:
        try:
            reg_req2 = RegisterDrvOutputRequest(realisation=realisation_to_register)
            await builder_store.call(reg_req2, suppress_last=True)
            print("CA realisation re-registered after restart!")
        except Exception as e:
            print(f"Re-registration FAILED: {e}")

    # -- Step 5: BuildDerivation with original path + resolved BasicDerivation --
    print()
    print("=" * 70)
    print("Step 5: BuildDerivation with original path + resolved derivation")
    print("=" * 70)

    build_req = BuildDerivationRequest(
        drv_path=deferred_drv_path,
        derivation=resolved_early,
    )

    print(f"Sending BuildDerivation for {deferred_drv_path}")
    print(f"  outputs: {[(n, o.path) for n, o in resolved_early.outputs.items()]}")

    try:
        resp = await builder_store.call(build_req)
        print(f"\nBuildDerivation result: status={resp.result.status}")
        print(f"  error_msg: {resp.result.error_msg!r}")
        print(f"  built_outputs: {resp.result.built_outputs}")
        if resp.result.status == 0:
            print("\n" + "=" * 70)
            print("SUCCESS!")
            print("=" * 70)
        else:
            print("\nFAILURE!")
    except Exception as e:
        print(f"\nBuildDerivation EXCEPTION: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()

    print(f"\nExpected output: {deferred_out_path}")

    await root_store.close()
    await builder_store.close()


if __name__ == "__main__":
    asyncio.run(main())
