"""Pytest markers for Nix bugs that make a test's subject unusable.

These are not "this test is flaky" escapes -- each one names an upstream
defect, the Nix versions that carry it, and the issue to check before
widening or dropping the exclusion.
"""

from __future__ import annotations

import pytest

NIX_GC_ROOTS_BUG = pytest.mark.nix_version(
    exclude=("2.34",),
    reason="findRoots/collectGarbage crash on nonnumeric temproots filenames; https://github.com/NixOS/nix/issues/16138",
)
"""``findRoots``/``collectGarbage`` abort instead of returning.

Nix creates temp-root files whose names are not PIDs, then parses every
temp-root filename with ``std::stoi``. Any test that reaches either call on
an affected version dies on the parse, not on anything the test did.
"""
