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

LINUX_FORK_THEN_INIT = pytest.mark.nix_platform("linux")
"""The test initialises Nix in a forked child, or asserts what `auto` picks.

`nix::initLibStore` calls `curl_global_init`, and curl calls
`SCDynamicStoreCopyProxies` there on macOS alone. SystemConfiguration is
CoreFoundation, which a process may not use between `fork` and `exec`, so the
child stops rather than fails. **A test that drives it hangs for the whole
step cap**, which is why the marker is worth more here than a skip usually is.

`resolve_worker_start` answers `spawn` on Darwin for the same reason, so the
rpc engine never reaches this. The inproc engine has no equivalent, because
the caller owns the fork. Issue #147.
"""

LINUX_STORE_EXEC = pytest.mark.nix_platform("linux")
"""The test runs a program that lives in a relocated store.

`nanopynix-store-exec` puts the real store at its logical path, and it does
that with an unprivileged user namespace and a bind mount. Neither one exists
on macOS, so `default.nix` builds the helper on Linux alone and
`store_exec_prefix` raises there instead of returning a prefix.

**The test cannot skip itself on the empty prefix.** Every store in this suite
is relocated, so the prefix is never empty and the call raises before the skip.
That loudness is deliberate -- a silent empty prefix reproduces the defect the
helper exists to correct. Issue #143.
"""
