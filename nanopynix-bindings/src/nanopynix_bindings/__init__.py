"""nanopynix compiled bindings namespace package.

The binding areas -- ``errors``, ``signals``, ``util``, ``store``, ``expr``,
``fetchers``, ``flake`` -- used to be separate extension modules. They are
submodules of one, ``_ext``, for the reason set out in
``src/nanopynix_modules.hh``: a function-local static in a header is a separate
object in every hidden-visibility ``.so`` that includes it, and that cost us a
real bug rather than just tidiness.

Each area still answers to its own dotted name. The one-line module beside this
file (``expr.py`` and friends) is what makes that work: importing it replaces
itself in ``sys.modules`` with the corresponding submodule of ``_ext``, so
``nanopynix_bindings.expr`` *is* ``_ext.expr`` -- the same object, not a
re-export of its contents. Those files also give a type checker a source module
to sit beside each generated ``.pyi``; a stub with no implementation is a
``reportMissingModuleSource`` warning at every import site in the repo.

Nothing here decides binding order any more. ``_ext``'s module init does, and
it runs on the first of the imports below. That is worth noticing: ``errors``
used to be listed first because importing it installs the single C++ -> Python
exception translator, and Python import order was the only thing sequencing
that. It is sequenced in C++ now, where an import sorter cannot reorder it.
"""

from __future__ import annotations

from . import (
    errors as errors,
    expr as expr,
    fetchers as fetchers,
    flake as flake,
    signals as signals,
    store as store,
    util as util,
)

__all__ = [
    "errors",
    "expr",
    "fetchers",
    "flake",
    "signals",
    "store",
    "util",
]
