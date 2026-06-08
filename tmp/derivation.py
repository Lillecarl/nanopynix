#! /usr/bin/env python3
"""
Reproducer: resolve deferred derivation via QueryRealisation.

  1. Read and parse ca-simple.drv
  2. Read and parse non-ca-depends-on-ca.drv
  3. Call compute_storepath on the parent (step 2) — should give its own path
  4. Compute hashDerivationModulo on child (step 1) for QueryRealisation
  5. QueryRealisation(drv_output="{drv_hash}!out") → get the realised output path
  6. Resolve the parent: remove inputDrvs, rewrite placeholders, add real path
  7. Fill in output paths (makeOutputPath from hashDerivationModulo)
  8. compute_storepath on fully-resolved parent → should equal the built .drv path
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from pynixd.config import LocalSocketStoreSpec
from pynixd.derivation_resolution import downstream_placeholder
from pynixd.drv_parser import read_drv_file
from pynixd.operations.ca_derivations import QueryRealisationRequest
from pynixd.store import LocalSocketStore
from pynixd.store_path import DrvOutput, StorePath
from pynixd.system_features import KNOWN_FEATURES
from pynixd.types import StoreId
from pynixd.utils import compress_hash, nix32_encode

# The two .drv files from the successful nix build
CHILD_DRV = "/nix/store/b6ilhhns20ckbafgmnck15sr9vzgf2vd-ca-simple.drv"
PARENT_DRV = "/nix/store/7c1khllp7rx48kwaxn14qp082ksb1lq6-non-ca-depends-on-ca.drv"

# Expected resolved (built) parent drv path and output
EXPECTED_RESOLVED = "/nix/store/3mkmm95y4lysgsp411syw5pfay333xi3-non-ca-depends-on-ca.drv"
EXPECTED_OUTPUT = "/nix/store/szyh41cj6c37vvqlbsmdmhka9hwzrybj-non-ca-depends-on-ca"


def make_output_path(drv_name: str, output_name: str, hex_hash: str) -> str:
    """Derive output store path from hashDerivationModulo (matching C++ makeOutputPath).

    C++: makeStorePath("output:<outputName>", hash, outputPathName(drvName, outputName))
    where hash.toString(Base16, true) gives "sha256:<hex>".
    """
    name = drv_name if output_name == "out" else f"{drv_name}-{output_name}"
    s = f"output:{output_name}:sha256:{hex_hash}:/nix/store:{name}"
    h = hashlib.sha256(s.encode()).digest()
    compressed = compress_hash(h, 20)
    return f"/nix/store/{nix32_encode(compressed)}-{name}"


async def main():
    # Step 0: connect to the system daemon
    store = LocalSocketStore(
        LocalSocketStoreSpec(
            store_id=StoreId("local"),
            feature_matrix={"x86_64-linux": set(KNOWN_FEATURES)},
            store_path=Path("/"),
        )
    )
    await store.start()
    print("Connected to system daemon\n")

    # Step 1: read and parse ca-simple.drv
    child = await read_drv_file(Path("/"), CHILD_DRV)
    assert child is not None, f"Failed to read {CHILD_DRV}"
    print(f"=== Child .drv: {CHILD_DRV} ===")
    print(f"  outputs: {[(o.name, o.path, o.hash_algo, o.hash_value) for o in child.outputs]}")
    print()

    # Step 2: read and parse non-ca-depends-on-ca.drv
    parent = await read_drv_file(Path("/"), PARENT_DRV)
    assert parent is not None, f"Failed to read {PARENT_DRV}"
    print(f"=== Parent .drv (original): {PARENT_DRV} ===")
    print(f"  outputs: {[(o.name, o.path, o.hash_algo, o.hash_value) for o in parent.outputs]}")
    print(f"  input_drvs: {[(str(k), v) for k, v in parent.input_drvs.items()]}")
    print(f"  args: {parent.args}")
    print()

    # Step 3: compute_storepath on raw parent (should give its own path)
    raw_storepath = parent.compute_storepath()
    print(f"=== Step 3: compute_storepath on raw parent ===")
    print(f"  Computed: {raw_storepath}")
    print(f"  Actual:   {PARENT_DRV}")
    print(f"  Match: {str(raw_storepath) == PARENT_DRV}")
    print()

    # Step 4: compute hashDerivationModulo on child
    child_hashes = child.hash_derivation_modulo(mask_outputs=True)
    print(f"=== Step 4: hashDerivationModulo on child ===")
    print(f"  Child hashes: {child_hashes}")
    print()

    # Step 5: QueryRealisation using the drv hash
    drv_hash = child_hashes["out"]
    drv_output = DrvOutput(hash_algo="sha256", hash_value=drv_hash, output_name="out")
    print(f"=== Step 5: QueryRealisation ===")
    print(f"  DrvOutput: {str(drv_output)!r}")
    resp = await store.call(QueryRealisationRequest(drv_output=drv_output))
    print(f"  Response: {resp}")

    if resp is None or not resp.realisations:
        print("  ERROR: No realisation found!")
        print("  (Did you run: nix build --file tests/nix ca.non_ca_depends_on_ca ...?)")
        await store.close()
        return

    out_path = resp.realisations[0].out_path
    print(f"  Output path: {out_path}")
    print()

    # Step 6: resolve — rewrite placeholders, move input_drvs to input_srcs
    ph = downstream_placeholder(StorePath(CHILD_DRV), "out")
    print(f"=== Step 6: Resolve parent ===")
    print(f"  Placeholder: {ph!r}")

    parent.input_drvs = {}
    parent.input_srcs.add(StorePath(str(out_path)))
    parent.args = [a.replace(ph, str(out_path)) for a in parent.args]
    print(f"  Resolved args: {parent.args}")
    print(f"  input_srcs now: {[str(p) for p in parent.input_srcs]}")
    print()

    # Step 7: fill in output paths via hashDerivationModulo
    resolved_hashes = parent.hash_derivation_modulo(mask_outputs=True)
    print(f"=== Step 7: Fill in output paths ===")
    print(f"  Resolved hashDerivationModulo: {resolved_hashes}")

    drv_name = parent.env.get("name", "unknown")
    for o in parent.outputs:
        hex_hash = resolved_hashes.get(o.name)
        if not hex_hash:
            print(f"  WARNING: no hash for output {o.name}")
            continue
        if o.path == "" and o.hash_algo == "" and o.hash_value == "":
            out_path = make_output_path(drv_name, o.name, hex_hash)
            o._path = out_path
            parent.env[o.name] = out_path
            print(f"  Filled {o.name}: {out_path}")

    print(f"  outputs now: {[(o.name, o.path, o.hash_algo, o.hash_value) for o in parent.outputs]}")
    print()

    # Step 8: compute_storepath on fully-resolved parent
    resolved_storepath = parent.compute_storepath()
    print(f"=== Step 8: compute_storepath on resolved parent ===")
    print(f"  Computed: {resolved_storepath}")
    print(f"  Expected: {EXPECTED_RESOLVED}")
    print(f"  MATCH: {str(resolved_storepath) == EXPECTED_RESOLVED}")

    if str(resolved_storepath) != EXPECTED_RESOLVED:
        serialized = parent.serialize()
        print(f"\n  Our ATerm ({len(serialized)} bytes):")
        print(f"    {serialized}")
        try:
            expected_content = Path(EXPECTED_RESOLVED).read_text()
            print(f"  Expected ATerm ({len(expected_content)} bytes):")
            print(f"    {expected_content}")
            if serialized == expected_content:
                print(f"  ✓ ATerms match byte-for-byte!")
            else:
                for i, (a, b) in enumerate(zip(serialized, expected_content)):
                    if a != b:
                        print(f"  First diff at byte {i}: {a!r} vs {b!r}")
                        print(f"  Context ours: ...{serialized[max(0,i-30):i+30]}...")
                        print(f"  Context exp:  ...{expected_content[max(0,i-30):i+30]}...")
                        break
        except FileNotFoundError:
            print(f"  (expected resolved .drv not found)")

    # Verify output path
    out_check = make_output_path(drv_name, "out", resolved_hashes["out"])
    print(f"\n=== Output path verification ===")
    print(f"  Computed: {out_check}")
    print(f"  Expected: {EXPECTED_OUTPUT}")
    print(f"  Match: {out_check == EXPECTED_OUTPUT}")

    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
