"""Reproduce the pytest process's startup side effects in exec'd subprocesses.

Python's `site` module imports `sitecustomize` at interpreter startup if this
directory is on `sys.path` (via the `PYTHONPATH` environment variable).
nanopynix's Nix worker is forked from a `multiprocessing` forkserver helper,
which is itself a freshly exec'd interpreter rather than a fork of the pytest
process, so it inherits neither of the two things set up below. Getting this
directory onto `PYTHONPATH` is handled by `nanopynix_testing.beartype_hook`,
which is loaded before anything else (see its docstring).

Both entries are no-ops unless their environment variable is set, so importing
this module is always safe -- including in the many subprocesses tests spawn
that have nothing to do with either concern.
"""

from __future__ import annotations

import os

import coverage

# `coverage.process_startup()` checks COVERAGE_PROCESS_START itself and is a
# no-op when that variable is unset. pytest-cov sets it when --cov is active.
coverage.process_startup()

# Without this, the forkserver child runs with NANOPYNIX_BEARTYPING inherited
# from the parent's environment -- so its `if TYPE_CHECKING or BEARTYPING:`
# imports are promoted -- but with no import hook installed, leaving the
# entire worker side of the rpc engine unchecked while inproc's equivalent is
# fully checked. Measured before this existed: the child reported
# `BEARTYPING = True` and `nanopynix actually type-checked = False`.
if os.environ.get("NANOPYNIX_BEARTYPING") == "1":
    import beartype_bootstrap

    beartype_bootstrap.install()
