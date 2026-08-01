"""nanopynix: Nix flake bindings (FlakeRef, lockFlake, callFlake).

Served by the merged `_ext` extension; see this package's `__init__` for why
the areas live in one shared object and how this file republishes one of them.
The assignment below is a module swap, not a re-export: CPython re-reads
`sys.modules[name]` after executing a module, so the importer that triggered
this gets `_ext.flake` itself.
"""

from __future__ import annotations

import sys

from . import _ext

sys.modules[__name__] = _ext.flake
