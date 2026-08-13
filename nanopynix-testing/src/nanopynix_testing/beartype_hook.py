"""Installs beartype's import hook before any of our own packages are imported.

Imported from the top of a suite's ``conftest.py``, ahead of everything else
there::

    importlib.import_module("nanopynix_testing.beartype_hook")

That import has to happen before ``import nanopynix`` and cannot be moved
anywhere later, which rules out both of the tidier-looking alternatives:

* pytest-beartype's own ``beartype_packages`` ini option applies the hook from
  its ``pytest_configure`` hookimpl. Every plugin's ``pytest_configure`` runs
  *after* initial-conftest collection, which is what imports conftest.py and
  therefore nanopynix. By then the target packages are imported and
  unhookable, and it silently no-ops with a "not checkable by beartype"
  warning.
* ``-p nanopynix_testing.beartype_hook`` in ``addopts`` is early enough --
  plugins named that way load during command-line parsing. It named
  ``tests.support.beartype_hook`` until issue #130, and that name did not
  resolve: ``-p`` is processed before the ``pythonpath`` ini setting and
  before rootdir reaches ``sys.path``, so the repository root was not
  importable yet and it died with ``No module named 'tests'``. **This module
  is an installed package now, so that objection no longer holds.** The
  conftest import stays because it is what every suite already does, and
  because one route is easier to reason about than two.

Being a conftest import makes the ordering fragile in one specific way:
``ruff check --fix`` alphabetizes that block, and ``nanopynix`` sorts before
``nanopynix_testing.beartype_hook`` -- exactly backwards. A conftest therefore
routes the side effect through ``importlib.import_module`` rather than an
import statement, which leaves isort nothing to reorder; see the comment
there.

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
