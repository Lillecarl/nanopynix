"""The heavy half of a subcommand, imported when that subcommand runs.

**clypi needs every command class before it can parse one argument.**
``pynix/__init__.py`` builds a union of the subcommands, and clypi evaluates
that annotation while the class body of ``Pynix`` runs. So every subcommand
module loads on every start, including ``pynix --help`` and every keypress of
a shell completion.

A subcommand module therefore holds the command class and its options, and
nothing else. What ``run`` needs lives here, and the module ``__getattr__``
below (PEP 562) imports it at the first attribute read -- which happens inside
``run``, after clypi decided that this is the subcommand to run.

Issue #123 measured what this saves, on the release build:

- ``pynix.repl`` was 91.8 ms, nearly all of it ``prompt_toolkit``;
- ``pynix.build`` was 112.2 ms, nearly all of it ``tree_sitter_nix``, which
  pulls ``tree_sitter_config`` and through it ``email_validator``.

``CLAUDE.md`` bans an import inside a function, and this is not one: the
mechanism is a package-level ``__getattr__``, the same one that
``nanopynix/__init__.py`` uses for its public names.
"""

from __future__ import annotations

import importlib
import types
import typing

if typing.TYPE_CHECKING:
    from pynix._impl import build as build, repl as repl

#: The implementation modules, one for each subcommand that has a heavy one.
_SUBMODULES: typing.Final[frozenset[str]] = frozenset({"build", "repl"})


def __getattr__(name: str) -> types.ModuleType:
    """Import an implementation module, and cache it in this namespace."""
    if name not in _SUBMODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"pynix._impl.{name}")
    globals()[name] = module
    return module
