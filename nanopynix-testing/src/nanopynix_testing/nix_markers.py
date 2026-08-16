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

LINUX_PROC_FS = pytest.mark.nix_platform("linux")
"""The test reads `/proc`, which no other operating system has.

Every use is a probe of the process itself that has no portable equivalent
here: the thread list under `/proc/self/task`, the descriptor list under
`/proc/self/fd`, and `oom_score_adj`. A macOS answer needs a different probe
in the bindings, not a branch in the test. Issue #143.
"""

LINUX_NAMESPACES = pytest.mark.nix_platform("linux")
"""The test makes a mount namespace or an overlay store.

`unshare`, `CLONE_NEWNS` and overlayfs are Linux. `nanopynix.namespace`
already refuses the whole feature on another operating system, and reports
the reason, so there is nothing for the test to assert there. Issue #143.
"""
