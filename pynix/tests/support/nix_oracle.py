"""The guard for a test that compares pynix against the `nix` command line.

Two tests here use the `nix` binary on the PATH as their oracle:
`test_flake_metadata.py` runs `nix flake metadata`, and `test_develop.py` runs
`nix print-dev-env`. Both compare the answer against what pynix produces
through the Nix that the bindings link.

**Those are two different Nix builds, and nothing said they had to agree.** A
developer machine can easily carry a newer `nix` than the one this checkout
links: 2.35.1 on the PATH against 2.34.8 in the bindings is what one macOS
machine had. `nix flake metadata` then reports a different `fingerprint`, and
the test says only that two long hashes differ. That is a defect of the test
and not of pynix, and it cost an afternoon to establish once.

So ask first, and say so plainly.
"""

from __future__ import annotations

import re
import shutil

import pytest

import nanopynix
from test_support.subprocess_output import run_process


def _version_of(text: str) -> str | None:
    match = re.search(r"(\d+(?:\.\d+)+)", text)
    return match.group(1) if match else None


async def require_matching_nix_cli() -> str:
    """Skip unless the `nix` on the PATH is the Nix that the bindings link.

    Returns the version the two agree on, for a test that wants to report it.

    Skips, rather than fails, for the reason the module docstring gives: a
    mismatch makes the comparison measure the difference between two Nix
    releases, which is not what any caller of this asked about. CI passes
    `-rsxXfE`, so the skip and its reason are printed there.
    """
    if shutil.which("nix") is None:
        pytest.skip("the nix CLI is the oracle for this test, and it is not on PATH")

    result = await run_process(["nix", "--version"])
    if result.returncode != 0:
        pytest.skip(f"the nix CLI is the oracle for this test, and `nix --version` exited {result.returncode}")

    cli = _version_of(result.stdout)
    linked = _version_of(str(nanopynix.build_info()["nix_version"]))  # type: ignore[reportUnknownArgumentType] -- extension lacks stubs
    if cli is None or linked is None:
        pytest.skip(f"cannot read both Nix versions to compare them: cli={cli!r} linked={linked!r}")
    if cli != linked:
        pytest.skip(
            f"the `nix` on PATH is {cli} and the bindings link {linked}. This test compares pynix against that "
            f"command, so a version difference would measure the two Nix releases against each other rather than "
            f"pynix. Put a matching Nix first on PATH to run it.",
        )
    return cli
