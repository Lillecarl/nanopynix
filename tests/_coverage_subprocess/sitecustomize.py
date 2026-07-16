"""Auto-start coverage.py measurement in freshly exec'd subprocesses.

Python's `site` module imports `sitecustomize` at interpreter startup if this
directory is on `sys.path` (via the `PYTHONPATH` environment variable).
nanopynix's Nix worker is forked from a `multiprocessing` forkserver helper,
which is itself a freshly exec'd interpreter rather than a plain subprocess,
so it never sees coverage's normal in-process instrumentation. Getting it
onto `PYTHONPATH` is handled by tests/conftest.py.

`coverage.process_startup()` checks `COVERAGE_PROCESS_START` itself and is a
no-op when that variable is unset, so importing this module is always safe.
"""

import coverage

coverage.process_startup()
