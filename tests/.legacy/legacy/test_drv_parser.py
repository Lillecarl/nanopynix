"""Stress-test the drv parser against .drv files in /nix/store.

Two test tiers:
1. test_parse_drv: parse every .drv, sanity-check structure (parametrized)
2. test_json_vs_nix: compare our JSON output against `nix derivation show`
   (sampled batch)
"""

from __future__ import annotations

import glob
import json
import random
import subprocess

import pytest
import structlog

from pynixd.drv_parser import parse_drv

log = structlog.get_logger(__name__)

# How many .drv files to compare against nix derivation show
JSON_SAMPLE_SIZE = 200
# nix derivation show can handle multiple paths at once — batch them
NIX_BATCH_SIZE = 50


def collect_drv_files() -> list[str]:
    """Find all .drv files in /nix/store."""
    return sorted(glob.glob("/nix/store/*.drv"))


DRV_FILES = collect_drv_files()

# Deterministic sample for JSON comparison
_rng = random.Random(42)
DRV_SAMPLE = _rng.sample(DRV_FILES, min(JSON_SAMPLE_SIZE, len(DRV_FILES)))


@pytest.mark.slow
@pytest.mark.parametrize(
    "drv_path", DRV_FILES, ids=[f.split("/")[-1] for f in DRV_FILES]
)
def test_parse_drv(drv_path: str) -> None:
    """Parse a single .drv file and sanity-check the result."""
    with open(drv_path) as f:
        content = f.read()

    parsed = parse_drv(content)

    # Basic structural sanity checks
    assert parsed.outputs, f"no outputs in {drv_path}"
    assert parsed.platform, f"no platform in {drv_path}"
    assert parsed.builder, f"no builder in {drv_path}"

    for output in parsed.outputs:
        assert output.name, f"unnamed output in {drv_path}"

    for input_drv in parsed.input_drvs:
        assert input_drv.startswith("/nix/store/"), (
            f"input_drv not a store path: {input_drv}"
        )

    for src in parsed.input_srcs:
        assert src.startswith("/nix/store/"), f"input_src not a store path: {src}"


def _nix_derivation_show(paths: list[str]) -> dict:
    """Call `nix derivation show` on a batch of paths."""
    result = subprocess.run(
        ["nix", "derivation", "show", *paths],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(f"nix derivation show failed: {result.stderr[:500]}")
    return json.loads(result.stdout)


@pytest.mark.slow
@pytest.mark.parametrize(
    "drv_path", DRV_SAMPLE, ids=[f.split("/")[-1] for f in DRV_SAMPLE]
)
def test_json_vs_nix(drv_path: str) -> None:
    """Compare our parser JSON output against `nix derivation show`."""
    with open(drv_path) as f:
        content = f.read()
    parsed = parse_drv(content)
    ours = parsed.to_json(drv_path)

    reference = _nix_derivation_show([drv_path])

    ours_inner = ours[drv_path]
    ref_inner = reference[drv_path]

    for field in (
        "system",
        "builder",
        "args",
        "name",
        "inputSrcs",
        "outputs",
        "env",
        "inputDrvs",
    ):
        assert ours_inner[field] == ref_inner[field], (
            f"{field} mismatch:\n"
            f"  ours: {json.dumps(ours_inner[field])[:300]}\n"
            f"  nix:  {json.dumps(ref_inner[field])[:300]}"
        )
