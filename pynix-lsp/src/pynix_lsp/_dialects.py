"""The registry of pluggable Nix dialects pynix-lsp has schema-aware support for.

This is the one place a dialect gets wired in or ripped out. Adding a new
dialect: implement ``Dialect`` (``_dialect.py``) in a new module, add one
instance to the list below. Removing one: delete its module, delete its
entry here -- nothing else in this package references a dialect by name.

Deliberately a plain, explicit Python list rather than plugin/entry-point
discovery: this matches the rest of the codebase's existing extensibility
style (e.g. ``pynix/__init__.py``'s ``Pynix.subcommand`` union of explicitly
imported ``clypi.Command`` subclasses), and keeps "which dialects are active"
fully static and greppable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from pynix_lsp._easykubenix import EasykubenixDialect
from pynix_lsp._module_system import ModuleSystemDialect
from pynix_lsp._terranix import TerranixDialect

if TYPE_CHECKING or BEARTYPING:
    from pynix_lsp._dialect import Dialect

DIALECTS: list[Dialect] = [ModuleSystemDialect(), TerranixDialect(), EasykubenixDialect()]
