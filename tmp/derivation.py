#! /usr/bin/env python3
"""
Tiny reproducer: verify Derivation round-trips (read→serialize), then
compute store path of the .drv file itself and compare with the real path.

Run from repo root with:  ./tmp/derivation.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

STORE_DIR = Path("/nix/store")

# Hardcoded from one `nix build` run
CA_DRV = STORE_DIR / "agazn184kb1ki5wz31810ga6yip2pxyi-ca-simple.drv"
PARENT_DRV_UNRESOLVED = STORE_DIR / "8snb293k8gdh59jwc511p2bfw72mjiwa-non-ca-depends-on-ca.drv"
PARENT_DRV_RESOLVED = STORE_DIR / "dcm47sig17dnkmdp3rx3lf02pfa1sl6n-non-ca-depends-on-ca.drv"


async def main():
    from pynixd.drv_parser import read_drv_file

    # read_drv_file expects store_path as root: Path("/") + "/nix/store/xxx.drv" → /nix/store/xxx.drv
    ROOT = Path("/")

    # ── 1. Round-trip on CA child ──
    raw = CA_DRV.read_text().rstrip("\n")
    drv = await read_drv_file(ROOT, str(CA_DRV))
    assert drv is not None, "Failed to parse ca-simple.drv"
    serialized = drv.serialize().rstrip("\n")

    print("=== CA child round-trip ===")
    print(f"Raw length:        {len(raw)}")
    print(f"Serialized length: {len(serialized)}")
    if raw == serialized:
        print("✓ EXACT MATCH (raw == serialize)")
    else:
        for i, (a, b) in enumerate(zip(raw, serialized)):
            if a != b:
                print(f"✗ First diff at offset {i}:")
                print(f"  raw[{i - 20}:{i + 20}] = {raw[max(0, i - 20) : i + 20]!r}")
                print(f"  ser[{i - 20}:{i + 20}] = {serialized[max(0, i - 20) : i + 20]!r}")
                break
        if len(raw) != len(serialized):
            print(f"  Length differs: {len(raw)} vs {len(serialized)}")

    # ── 2. Compute store path and compare ──
    computed_path = drv.compute_storepath()
    actual_path = CA_DRV
    print("\n=== CA child store path ===")
    print(f"Computed: {computed_path}")
    print(f"Actual:   {actual_path}")
    print(f"  name from env: {drv.env.get('name', 'NOT SET')!r}")
    print(f"✓ MATCH: {computed_path == actual_path}")

    # ── 3. Round-trip on unresolved parent ──
    raw_p = PARENT_DRV_UNRESOLVED.read_text().rstrip("\n")
    drv_p = await read_drv_file(ROOT, str(PARENT_DRV_UNRESOLVED))
    assert drv_p is not None, "Failed to parse parent .drv"
    serialized_p = drv_p.serialize().rstrip("\n")

    print("\n=== Parent (unresolved) round-trip ===")
    print(f"Raw length:        {len(raw_p)}")
    print(f"Serialized length: {len(serialized_p)}")
    if raw_p == serialized_p:
        print("✓ EXACT MATCH (raw == serialize)")
    else:
        for i, (a, b) in enumerate(zip(raw_p, serialized_p)):
            if a != b:
                print(f"✗ First diff at offset {i}:")
                print(f"  raw[{i - 20}:{i + 20}] = {raw_p[max(0, i - 20) : i + 20]!r}")
                print(f"  ser[{i - 20}:{i + 20}] = {serialized_p[max(0, i - 20) : i + 20]!r}")
                break
        if len(raw_p) != len(serialized_p):
            print(f"  Length differs: {len(raw_p)} vs {len(serialized_p)}")

    # Compute store path for unresolved parent
    computed_path_p = drv_p.compute_storepath()
    actual_path_p = PARENT_DRV_UNRESOLVED
    print("\n=== Parent (unresolved) store path ===")
    print(f"Computed: {computed_path_p}")
    print(f"Actual:   {actual_path_p}")
    print(f"  name from env: {drv_p.env.get('name', 'NOT SET')!r}")
    print(f"✓ MATCH: {computed_path_p == actual_path_p}")

    # ── 4. Round-trip on resolved parent ──
    raw_r = PARENT_DRV_RESOLVED.read_text().rstrip("\n")
    drv_r = await read_drv_file(ROOT, str(PARENT_DRV_RESOLVED))
    assert drv_r is not None, "Failed to parse resolved .drv"
    serialized_r = drv_r.serialize().rstrip("\n")

    print("\n=== Parent (resolved) round-trip ===")
    print(f"Raw length:        {len(raw_r)}")
    print(f"Serialized length: {len(serialized_r)}")
    if raw_r == serialized_r:
        print("✓ EXACT MATCH (raw == serialize)")
    else:
        for i, (a, b) in enumerate(zip(raw_r, serialized_r)):
            if a != b:
                print(f"✗ First diff at offset {i}:")
                print(f"  raw[{i - 20}:{i + 20}] = {raw_r[max(0, i - 20) : i + 20]!r}")
                print(f"  ser[{i - 20}:{i + 20}] = {serialized_r[max(0, i - 20) : i + 20]!r}")
                break
        if len(raw_r) != len(serialized_r):
            print(f"  Length differs: {len(raw_r)} vs {len(serialized_r)}")

    computed_path_r = drv_r.compute_storepath()
    actual_path_r = PARENT_DRV_RESOLVED
    print("\n=== Parent (resolved) store path ===")
    print(f"Computed: {computed_path_r}")
    print(f"Actual:   {actual_path_r}")
    print(f"  name from env: {drv_r.env.get('name', 'NOT SET')!r}")
    print(f"✓ MATCH: {computed_path_r == actual_path_r}")


if __name__ == "__main__":
    asyncio.run(main())
