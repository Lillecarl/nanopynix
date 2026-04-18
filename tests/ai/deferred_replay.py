"""Standalone test for deferred CA derivation building via BuildDerivation.

This script simulates what pynixd does in a controlled, observable way:
1. Build the derivation against the root store (reference result)
2. Create a fresh test store (simulating the builder)
3. Transfer needed paths from root -> test
4. Register CA realisations on the test store
5. Send BuildDerivation to the test store
6. Compare the result

This allows fast iteration on the wire protocol interaction without
the full pynixd proxy stack.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from pynixd.drv_parser import read_drv_file, to_basic_derivation
from pynixd.operations.base import BuildMode
from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.operations.ca_derivations import (
    QueryRealisationRequest,
    RegisterDrvOutputRequest,
)
from pynixd.operations.query_valid_paths import QueryValidPathsRequest
from pynixd.operations.query_derivation_output_map import (
    QueryDerivationOutputMapRequest,
)
from pynixd.operations.query_derivation_outputs_batch import (
    QueryDerivationOutputsBatchRequest,
)
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


async def main() -> None:
    # -- Step 1: Build against root store to get reference paths --
    print("=" * 70)
    print("Step 1: Build against root store")
    print("=" * 70)

    root_path = STORE_PREFIX / "deferred-replay-root"
    rmtree_robust(root_path)

    root_kwargs = get_test_store_kwargs(
        extra_args=CA_EXTRA_ARGS,
        extra_env=CA_NIX_CONFIG,
    )
    root_store = LocalSocketStore(
        id="deferred-replay-root",
        store_path=root_path,
        **root_kwargs,
    )
    await root_store.ensure_daemon()

    # Build CA dependency first
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
    rc, stdout, stderr, stdboth = await run_subproc(cmd_ca, nix_config=CA_NIX_CONFIG)
    assert rc == 0, f"CA build failed: {stdboth}"
    ca_out_path = stdout.strip()
    print(f"CA output: {ca_out_path}")

    # Build deferred derivation
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
    rc, stdout, stderr, stdboth = await run_subproc(
        cmd_deferred, nix_config=CA_NIX_CONFIG
    )
    assert rc == 0, f"Deferred build failed: {stdboth}"
    deferred_out_path = stdout.strip()
    print(f"Deferred output: {deferred_out_path}")

    # -- Step 2: Parse the .drv files --
    print()
    print("=" * 70)
    print("Step 2: Parse .drv files")
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

    print(f"\nCA .drv outputs: {ca_parsed.output_paths()}")
    print(f"CA .drv input_drvs: {list(ca_parsed.input_drvs.keys())}")
    print(f"CA .drv input_srcs: {sorted(str(p) for p in ca_parsed.input_srcs)}")

    print(f"\nDeferred .drv outputs: {deferred_parsed.output_paths()}")
    print(f"Deferred .drv input_drvs: {list(deferred_parsed.input_drvs.keys())}")
    print(
        f"Deferred .drv input_srcs: {sorted(str(p) for p in deferred_parsed.input_srcs)}"
    )

    for o in deferred_parsed.outputs:
        print(
            f"  output: name={o.name} path={o.path!r} hash_algo={o.hash_algo!r} hash_value={o.hash_value!r}"
        )

    # -- Step 3: Create test store (our "builder") --
    print()
    print("=" * 70)
    print("Step 3: Create test store")
    print("=" * 70)

    test_path = STORE_PREFIX / "deferred-replay-test"
    rmtree_robust(test_path)

    test_kwargs = get_test_store_kwargs(
        extra_args=CA_EXTRA_ARGS,
        extra_env=CA_NIX_CONFIG,
    )
    test_store = LocalSocketStore(
        id="deferred-replay-test",
        store_path=test_path,
        **test_kwargs,
    )
    await test_store.ensure_daemon()

    # -- Step 4: Determine what to transfer and send it --
    print()
    print("=" * 70)
    print("Step 4: Transfer paths to test store")
    print("=" * 70)

    paths_to_transfer: set[StorePath] = set()

    # CA derivation: drv + input_srcs + output
    paths_to_transfer.add(ca_drv_path)
    paths_to_transfer.update(ca_parsed.input_srcs)
    paths_to_transfer.add(StorePath(ca_out_path))

    # Deferred derivation: drv + input_srcs + input_drvs' drv paths
    paths_to_transfer.add(deferred_drv_path)
    paths_to_transfer.update(deferred_parsed.input_srcs)
    for input_drv in deferred_parsed.input_drvs:
        paths_to_transfer.add(input_drv)

    print(f"Paths to transfer ({len(paths_to_transfer)}):")
    for p in sorted(str(p) for p in paths_to_transfer):
        print(f"  {p}")

    missing_before = paths_to_transfer - test_store.tracker.known_paths
    print(f"\nMissing from test store: {len(missing_before)}")

    await LocalSocketStore.stream_paths_store_to_store(
        root_store, test_store, paths_to_transfer
    )

    # Verify
    valid_resp = await test_store.execute(
        QueryValidPathsRequest(paths=paths_to_transfer, substitute=0)
    )
    print(
        f"Test store now has {len(valid_resp.paths)} of {len(paths_to_transfer)} paths"
    )
    missing_after = paths_to_transfer - valid_resp.paths
    if missing_after:
        print(f"STILL MISSING: {sorted(str(p) for p in missing_after)}")

    # -- Step 5: Get and register CA realisation on test store --
    print()
    print("=" * 70)
    print("Step 5: Get and register CA realisation on test store")
    print("=" * 70)

    # First, query the output map from root to discover the drvHash
    outmap_resp = await root_store.execute(
        QueryDerivationOutputMapRequest(path=ca_drv_path)
    )
    print(f"CA derivation output map: {outmap_resp.items}")

    # Get the realisation via nix realisation info
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
    rc, realisation_out, realisation_err, _ = await run_subproc(
        realisation_cmd, nix_config=CA_NIX_CONFIG, expected_retcode=None
    )
    if rc == 0 and realisation_out.strip():
        realisations_raw = json.loads(realisation_out)
        print(
            f"Realisation info (CLI): {json.dumps(realisations_raw, indent=2)[:1000]}"
        )
    else:
        print(f"nix realisation info failed (rc={rc}): {realisation_err}")
        realisations_raw = []

    # Also query via the wire protocol (QueryRealisationRequest)
    # We need the DrvOutput = "sha256:hash!outName" for the CA derivation
    # Get the drvHash from the output map items or from the realisation info
    drv_hash: str | None = None
    if realisations_raw:
        for r in realisations_raw:
            if "id" in r:
                drv_hash_candidate = r["id"]
                print(f"  Found DrvOutput from CLI: {drv_hash_candidate}")
                drv_hash = drv_hash_candidate
                break

    # If CLI didn't work, try querying via wire protocol
    if not drv_hash:
        # Try querying the root store's DB directly
        root_db_path = root_path / "nix" / "var" / "nix" / "db" / "db.sqlite"
        if root_db_path.exists():
            root_conn = sqlite3.connect(str(root_db_path))
            rows = root_conn.execute(
                "SELECT id, drvHash, outputName, outputId, realisation FROM Realisations"
            ).fetchall()
            print(f"Root store DB has {len(rows)} realisations:")
            for row in rows:
                print(f"  {row}")
                # Try to construct the DrvOutput from drvHash + outputName
            root_conn.close()

    # Now query via wire protocol using the DrvOutput
    if drv_hash:
        print(f"\nQuerying realisation via wire: {drv_hash}")
        try:
            qresp = await root_store.execute(
                QueryRealisationRequest(drv_output=drv_hash)
            )
            print(f"Wire realisation response: {qresp.realisations}")
            if qresp.realisations:
                realisation_to_register = qresp.realisations[0]
                print(f"  Realisation: {json.dumps(realisation_to_register, indent=2)}")
            else:
                print("  No realisations returned from wire query!")
                realisation_to_register = None
        except Exception as e:
            print(f"  Wire query failed: {e}")
            realisation_to_register = None
    else:
        print("\nCould not determine DrvOutput for CA derivation!")
        realisation_to_register = None

    # If we still don't have it, construct from what we know
    if not realisation_to_register:
        # Try constructing from the DB directly
        root_db_path = root_path / "nix" / "var" / "nix" / "db" / "db.sqlite"
        if root_db_path.exists():
            root_conn = sqlite3.connect(str(root_db_path))
            rows = root_conn.execute("SELECT id FROM Realisations").fetchall()
            print(f"Root DB Realisation IDs: {[r[0] for r in rows]}")
            root_conn.close()

        # Fallback: construct realisation manually from what we know
        ca_out_basename = ca_out_path.rsplit("/", 1)[-1]
        print(f"\nAttempting to construct realisation manually...")

        # Try to get the drvHash from the derivation output map response
        # The QueryDerivationOutputMap returns {output_name: StorePath | None}
        # The DrvOutput needs the hash of the derivation modulo
        # We can try to compute it from nix hash-path or just query it
        print("  Trying: nix derivation show to compute drvHash")

        drv_show_cmd = [
            NIX_BIN,
            "derivation",
            "show",
            "--store",
            str(root_path),
            str(ca_drv_path),
        ]
        rc, drv_show_out, _, _ = await run_subproc(
            drv_show_cmd, nix_config=CA_NIX_CONFIG, expected_retcode=None
        )
        if rc == 0:
            drv_json = json.loads(drv_show_out)
            print(f"  Derivation JSON keys: {list(drv_json.keys())}")
            # The drvHash is computed from hashDerivationModulo which is internal
            # We can't easily compute it ourselves, but we can query it

        # Last resort: query QueryDerivationOutputMap which internally
        # uses drvHash. Let's try a different approach - query the DB.
        root_db_path = root_path / "nix" / "var" / "nix" / "db" / "db.sqlite"
        if root_db_path.exists():
            root_conn = sqlite3.connect(str(root_db_path))
            # Check all tables to understand schema
            tables = root_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            print(f"  DB tables: {[t[0] for t in tables]}")

            # Try Nix 2.34 schema: Realisations table
            try:
                rows = root_conn.execute("SELECT * FROM Realisations").fetchall()
                print(f"  Realisations rows: {rows}")
            except Exception:
                pass

            # Try DrvOutputs table (older Nix versions)
            try:
                rows = root_conn.execute("SELECT * FROM DrvOutputs").fetchall()
                print(f"  DrvOutputs rows: {rows}")
            except Exception:
                pass

            root_conn.close()

        realisation_to_register = None

    # Register the realisation on the test store
    if realisation_to_register:
        print(f"\nRegistering realisation on test store...")
        print(f"  Realisation: {json.dumps(realisation_to_register, indent=2)}")
        try:
            reg_req = RegisterDrvOutputRequest(realisation=realisation_to_register)
            await test_store.call(reg_req, suppress_last=True)
            print("  Realisation registered successfully!")
        except Exception as e:
            print(f"  Registration FAILED: {e}")
    else:
        print("\nWARNING: No realisation to register! Will try building anyway.")

    # Verify registration on test store
    test_db_path = test_path / "nix" / "var" / "nix" / "db" / "db.sqlite"
    if test_db_path.exists():
        test_conn = sqlite3.connect(str(test_db_path))
        try:
            rows = test_conn.execute("SELECT * FROM Realisations").fetchall()
            print(f"Test store DB realisations: {len(rows)}")
            for r in rows:
                print(f"  {r}")
        except Exception:
            pass
        try:
            rows = test_conn.execute("SELECT * FROM DrvOutputs").fetchall()
            print(f"Test store DB DrvOutputs: {len(rows)}")
            for r in rows:
                print(f"  {r}")
        except Exception:
            pass
        test_conn.close()

    # Also verify the output map on test store
    try:
        test_outmap = await test_store.execute(
            QueryDerivationOutputMapRequest(path=ca_drv_path)
        )
        print(f"Test store output map for CA drv: {test_outmap.items}")
    except Exception as e:
        print(f"Test store output map query failed: {e}")

    # Critical diagnostic: verify the output map for the DEFERRED drv
    # This is what the daemon internally queries to resolve $out
    try:
        deferred_outmap = await test_store.execute(
            QueryDerivationOutputMapRequest(path=deferred_drv_path)
        )
        print(f"Test store output map for DEFERRED drv: {deferred_outmap.items}")
    except Exception as e:
        print(f"Test store output map query for deferred drv failed: {e}")

    # Compare: what does the root store return for the deferred drv?
    try:
        root_deferred_outmap = await root_store.execute(
            QueryDerivationOutputMapRequest(path=deferred_drv_path)
        )
        print(f"Root store output map for DEFERRED drv: {root_deferred_outmap.items}")
    except Exception as e:
        print(f"Root store output map query for deferred drv failed: {e}")

    # Also check if the deferred drv is valid on the test store
    from pynixd.operations.is_valid_path import IsValidPathRequest

    try:
        is_valid = await test_store.execute(IsValidPathRequest(path=deferred_drv_path))
        print(f"Deferred .drv is valid on test store: {is_valid.valid}")
    except Exception as e:
        print(f"IsValidPath check failed: {e}")

    # -- Step 6: Build the deferred derivation via BuildDerivation --
    print()
    print("=" * 70)
    print("Step 6: Build deferred derivation via BuildDerivation")
    print("=" * 70)

    # Create BasicDerivation from parsed .drv
    output_cache_resp = await root_store.execute(
        QueryDerivationOutputsBatchRequest(
            drv_paths=set(deferred_parsed.input_drvs.keys())
        )
    )
    output_cache = output_cache_resp.outputs if output_cache_resp.outputs else {}

    basic = to_basic_derivation(
        deferred_parsed, root_store.store_path, output_cache=output_cache
    )

    print(f"BasicDerivation input_srcs ({len(basic.input_srcs)}):")
    for p in sorted(str(p) for p in basic.input_srcs):
        print(f"  {p}")
    print(f"BasicDerivation outputs:")
    for name, o in basic.outputs.items():
        print(f"  {name}: path={o.path!r} method={o.method!r} hash={o.hash_digest!r}")

    # Now add the .drv paths to input_srcs (what _patch_deferred_inputs does)
    added: set[StorePath] = set()

    # Add the deferred .drv itself
    if deferred_drv_path not in basic.input_srcs:
        basic.input_srcs.add(deferred_drv_path)
        added.add(deferred_drv_path)

    # Add all input_drvs .drv paths
    for input_drv in deferred_parsed.input_drvs:
        if input_drv not in basic.input_srcs:
            basic.input_srcs.add(input_drv)
            added.add(input_drv)

    # Add the CA output path
    ca_out_sp = StorePath(ca_out_path).with_store_prefix()
    if ca_out_sp not in basic.input_srcs:
        basic.input_srcs.add(ca_out_sp)
        added.add(ca_out_sp)

    if added:
        print(f"\nAdded to input_srcs: {sorted(str(p) for p in added)}")

    # Transfer any newly added paths to test store
    new_missing = added - test_store.tracker.known_paths
    if new_missing:
        print(f"Transferring {len(new_missing)} additional paths to test store")
        await LocalSocketStore.stream_paths_store_to_store(
            root_store, test_store, new_missing
        )

    # Send BuildDerivation
    build_req = BuildDerivationRequest(
        drv_path=deferred_drv_path,
        derivation=basic,
    )

    print(f"\nSending BuildDerivation for {deferred_drv_path}")
    print(f"  input_srcs: {sorted(str(p) for p in basic.input_srcs)}")
    try:
        resp = await test_store.call(build_req)
        print(f"\nBuildDerivation result: status={resp.result.status}")
        print(f"  error_msg: {resp.result.error_msg}")
        print(f"  built_outputs: {resp.result.built_outputs}")
        if resp.result.status == 0:
            print("\nSUCCESS!")
        else:
            print("\nFAILURE!")
    except Exception as e:
        print(f"\nBuildDerivation EXCEPTION: {type(e).__name__}: {e}")

    # -- Step 7: Alternative - Transfer resolved .drv and BuildPaths --
    print()
    print("=" * 70)
    print("Step 7: Alternative - Transfer resolved .drv + BuildPaths")
    print("=" * 70)
    print("The root store also has a RESOLVED .drv file for the deferred")
    print("derivation. We transfer this + the output path and try BuildPaths.")
    print()

    # Get the resolved .drv path from root store DB
    root_db_path = root_path / "nix" / "var" / "nix" / "db" / "db.sqlite"
    root_conn = sqlite3.connect(str(root_db_path))
    resolved_rows = root_conn.execute(
        "SELECT path FROM ValidPaths WHERE path LIKE '%non-ca-depends-on-ca.drv' AND path != ?",
        (str(deferred_drv_path),),
    ).fetchall()
    root_conn.close()

    if resolved_rows:
        resolved_drv_path = StorePath(resolved_rows[0][0])
        print(f"Resolved .drv: {resolved_drv_path}")
    else:
        resolved_drv_path = None
        print("No resolved .drv found in root store!")

    from pynixd.operations.is_valid_path import IsValidPathRequest
    from pynixd.operations.build_paths import BuildPathsRequest
    from pynixd.derived_path import DerivedPath

    if resolved_drv_path:
        test3_path = STORE_PREFIX / "deferred-replay-test3"
        rmtree_robust(test3_path)

        test3_store = LocalSocketStore(
            id="deferred-replay-test3",
            store_path=test3_path,
            **test_kwargs,
        )
        await test3_store.ensure_daemon()

        # Transfer: original paths + resolved .drv + deferred output
        extra_paths = {resolved_drv_path, StorePath(deferred_out_path)}
        all_paths3 = paths_to_transfer | extra_paths

        await LocalSocketStore.stream_paths_store_to_store(
            root_store, test3_store, all_paths3
        )

        # Register CA realisation
        if realisation_to_register:
            try:
                reg_req3 = RegisterDrvOutputRequest(realisation=realisation_to_register)
                await test3_store.call(reg_req3, suppress_last=True)
                print("CA realisation registered on test3 store!")
            except Exception as e:
                print(f"Registration on test3 FAILED: {e}")

        # Check resolution
        try:
            is_valid3 = await test3_store.execute(
                IsValidPathRequest(path=deferred_drv_path)
            )
            print(f"Original deferred .drv valid: {is_valid3.valid}")
        except Exception as e:
            print(f"IsValidPath failed: {e}")

        try:
            is_valid3r = await test3_store.execute(
                IsValidPathRequest(path=resolved_drv_path)
            )
            print(f"Resolved .drv valid: {is_valid3r.valid}")
        except Exception as e:
            print(f"IsValidPath for resolved failed: {e}")

        try:
            t3_outmap = await test3_store.execute(
                QueryDerivationOutputMapRequest(path=deferred_drv_path)
            )
            print(f"Test3 output map for ORIGINAL deferred drv: {t3_outmap.items}")
        except Exception as e:
            print(f"Output map for original failed: {e}")

        try:
            t3r_outmap = await test3_store.execute(
                QueryDerivationOutputMapRequest(path=resolved_drv_path)
            )
            print(f"Test3 output map for RESOLVED drv: {t3r_outmap.items}")
        except Exception as e:
            print(f"Output map for resolved failed: {e}")

        # Try BuildPaths with original deferred drv
        deferred_dp = DerivedPath(f"{deferred_drv_path}!out")
        print(f"\nAttempt 1: BuildPaths with original deferred drv...")

        bp_req1 = BuildPathsRequest(
            derived_paths={deferred_dp},
            build_mode=BuildMode.NORMAL,
        )
        try:
            bp_resp1 = await test3_store.call(bp_req1)
            print(f"BuildPaths result: value={bp_resp1.value}")
        except Exception as e:
            print(f"BuildPaths EXCEPTION: {type(e).__name__}: {e}")

        # Try BuildPaths with resolved drv
        resolved_dp = DerivedPath(f"{resolved_drv_path}!out")
        print(f"\nAttempt 2: BuildPaths with RESOLVED drv...")

        bp_req2 = BuildPathsRequest(
            derived_paths={resolved_dp},
            build_mode=BuildMode.NORMAL,
        )
        try:
            bp_resp2 = await test3_store.call(bp_req2)
            print(f"BuildPaths result: value={bp_resp2.value}")
        except Exception as e:
            print(f"BuildPaths EXCEPTION: {type(e).__name__}: {e}")

        await test3_store.close()
    else:
        print("Skipping - no resolved .drv found")

    # -- Step 8: Alternative - BuildDerivation with resolved .drv --
    print()
    print("=" * 70)
    print("Step 8: Alternative - BuildDerivation with resolved .drv")
    print("=" * 70)
    print("Send BuildDerivation pointing at the RESOLVED .drv instead.")
    print()

    if resolved_drv_path:
        test4_path = STORE_PREFIX / "deferred-replay-test4"
        rmtree_robust(test4_path)

        test4_store = LocalSocketStore(
            id="deferred-replay-test4",
            store_path=test4_path,
            **test_kwargs,
        )
        await test4_store.ensure_daemon()

        # Transfer: original paths + resolved .drv + deferred output
        extra_paths4 = {resolved_drv_path, StorePath(deferred_out_path)}
        all_paths4 = paths_to_transfer | extra_paths4

        await LocalSocketStore.stream_paths_store_to_store(
            root_store, test4_store, all_paths4
        )

        # Register CA realisation
        if realisation_to_register:
            try:
                reg_req4 = RegisterDrvOutputRequest(realisation=realisation_to_register)
                await test4_store.call(reg_req4, suppress_last=True)
                print("CA realisation registered on test4 store!")
            except Exception as e:
                print(f"Registration on test4 FAILED: {e}")

        # Parse the resolved .drv and build a BasicDerivation from it
        resolved_parsed = read_drv_file(root_store.store_path, resolved_drv_path)
        print(f"Resolved .drv outputs: {resolved_parsed.output_paths()}")
        print(f"Resolved .drv input_drvs: {list(resolved_parsed.input_drvs.keys())}")
        print(
            f"Resolved .drv input_srcs: {sorted(str(p) for p in resolved_parsed.input_srcs)}"
        )
        for o in resolved_parsed.outputs:
            print(
                f"  output: name={o.name} path={o.path!r} hash_algo={o.hash_algo!r} hash_value={o.hash_value!r}"
            )

        resolved_basic = to_basic_derivation(resolved_parsed, root_store.store_path)

        print(f"\nBasicDerivation from resolved .drv:")
        print(f"  input_srcs ({len(resolved_basic.input_srcs)}):")
        for p in sorted(str(p) for p in resolved_basic.input_srcs):
            print(f"    {p}")
        print(f"  outputs:")
        for name, o in resolved_basic.outputs.items():
            print(
                f"    {name}: path={o.path!r} method={o.method!r} hash={o.hash_digest!r}"
            )

        # Send BuildDerivation pointing at the RESOLVED .drv
        build_req4 = BuildDerivationRequest(
            drv_path=resolved_drv_path,
            derivation=resolved_basic,
        )

        print(f"\nSending BuildDerivation for RESOLVED {resolved_drv_path}")
        try:
            resp4 = await test4_store.call(build_req4)
            print(f"\nBuildDerivation result: status={resp4.result.status}")
            print(f"  error_msg: {resp4.result.error_msg}")
            if resp4.result.status == 0:
                print("\nSUCCESS!")
            else:
                print("\nFAILURE!")
        except Exception as e:
            print(f"\nBuildDerivation EXCEPTION: {type(e).__name__}: {e}")

        await test4_store.close()
    else:
        print("Skipping - no resolved .drv found")

    # -- Step 8: Summary --
    print()
    print("=" * 70)
    print(f"Expected output: {deferred_out_path}")
    print("=" * 70)

    await root_store.close()
    await test_store.close()


if __name__ == "__main__":
    asyncio.run(main())
