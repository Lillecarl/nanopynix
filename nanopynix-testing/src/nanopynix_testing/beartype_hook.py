"""Installs beartype's import hook before any of our own packages are imported.

Each suite loads it from its own ``pytest.ini``::

    addopts = -p nanopynix_testing.beartype_hook

A ``-p`` plugin loads during command-line parsing, which is earlier than the
first conftest, so no conftest has to order its own imports around the hook.
The two alternatives are both worse, and one of them does not work at all:

* pytest-beartype's own ``beartype_packages`` ini option applies the hook from
  its ``pytest_configure`` hookimpl. Every plugin's ``pytest_configure`` runs
  *after* initial-conftest collection, which is what imports conftest.py and
  therefore nanopynix. By then the target packages are imported and
  unhookable, and it silently no-ops with a "not checkable by beartype"
  warning.
* An ``importlib.import_module`` call at the top of a ``conftest.py`` is early
  enough, and every suite took that route until issue #130. It leaves the
  ordering fragile in one specific way: ``ruff check --fix`` alphabetizes an
  import block, and ``nanopynix`` sorts before this module -- exactly
  backwards. It also cannot serve a conftest that imports the instrumented
  package at its own top, which ``pynix/tests/conftest.py`` does.

``-p`` was not available until issue #130. The plugin name was
``tests.support.beartype_hook``, and that name did not resolve: ``-p`` is
processed before the ``pythonpath`` ini setting and before rootdir reaches
``sys.path``, so the repository root was not importable yet and it died with
``No module named 'tests'``. This module is an installed package now, and an
installed package is importable from the first line of the run.

**A suite that loses the hook still reports every test as passed.** The
failure is silent, which is why the registration is one line in one file per
project rather than a convention. ``tests/nanopynix/test_beartype_instrumentation.py``
is what catches its absence.

This module also puts ``_subprocess_startup`` beside it on ``PYTHONPATH`` so
that freshly exec'd subprocesses -- above all the multiprocessing forkserver
helper the Nix worker is forked from -- run the same instrumentation instead
of inheriting only the environment variable. It is the sole writer of that
variable; coverage.py's subprocess shim lives in the same directory and is
picked up by the same entry, which is why no conftest arranges it separately.

**The directory holds no ``__init__.py``, and that is deliberate.**
``sitecustomize`` has to be importable by the ``site`` module at interpreter
startup, which reads ``sys.path`` and knows nothing of packages here, so the
directory itself goes on the path. It ships inside this package only so that
an installed copy can find it: this module computes the location from its own
``__file__``, and never from a checkout.

The package list and beartype config live in ``beartype_bootstrap`` beside
that shim, so this module and the shim cannot drift apart.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SUBPROCESS_STARTUP_DIR = str(Path(__file__).resolve().parent / "_subprocess_startup")

# Importable here for the same reason `sitecustomize` can import it there: the
# directory is on the path. Prepending rather than appending keeps it ahead of
# any same-named module further along.
if _SUBPROCESS_STARTUP_DIR not in sys.path:
    sys.path.insert(0, _SUBPROCESS_STARTUP_DIR)

# Children need the directory too, and they get it the only way a freshly
# exec'd interpreter can: the environment.
_existing_pythonpath = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = os.pathsep.join(
    [_SUBPROCESS_STARTUP_DIR, *([_existing_pythonpath] if _existing_pythonpath else [])]
)

import beartype_bootstrap  # noqa: E402 -- importable only after the sys.path insertion above

beartype_bootstrap.install()
