"""The three evaluation options, with the completers that this program has.

``libpynix.nix_options`` declares ``--file``, ``--flake`` and ``--attr`` and
supplies no completer, because answering a Tab after ``--attr`` means
evaluating Nix and a library that declares an option has no evaluator. This
module is the join: it takes the declarations from there and the completers
from :mod:`pynix._attr_completion`, and every command module of ``pynix``
imports the three from here rather than from ``libpynix``.

**The import costs nothing that a start does not already pay.**
``_attr_completion`` imports the standard library alone at module level, and
does every import that reaches the evaluator inside the function that a
completion calls. So a command that lists an option pays for a function object
and not for ``pynix.target``. ``tests/meta/test_import_budget.py`` is what
keeps that true.

**``--flake`` names its search, and each command names a different one.**
``nix develop F#<TAB>`` offers what is under ``devShells.<system>`` and ``nix
build F#<TAB>`` does not, because the two commands override
`getDefaultFlakeAttrPathPrefixes` differently. So :func:`flake_option` takes
the name of the search, and a command module passes the one it already passes
to :func:`~pynix.target.evaluate_target` at run time. A name and not the search
itself: building one means importing ``pynix.target``, which is the 101 ms that
this module exists to keep off a start that evaluates nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from libpynix import attr_option as _attr_option, file_option as _file_option, flake_option as _flake_option
from nanopynix._typechecking import no_runtime_type_check
from pynix._attr_completion import complete_attr, complete_file, flake_completer

if TYPE_CHECKING:
    from pynix._attr_completion import FlakeSearch


@no_runtime_type_check  # a declaration returns a Spec, not the annotated type; see libpynix.nix_options
def file_option() -> str | None:
    """``--file``, completing an attribute path after a ``#``."""
    return _file_option(complete=complete_file)


@no_runtime_type_check  # see file_option
def attr_option() -> str | None:
    """``--attr``, completing against the ``--file`` beside it."""
    return _attr_option(complete=complete_attr)


@no_runtime_type_check  # see file_option
def flake_option(*, search: FlakeSearch = "base") -> str | None:
    """``--flake``, completing a fragment against the search *search* names.

    ``"base"`` is the pair of `SourceExprCommand`, which is what `nix build`,
    `nix eval` and `nix derivation show` use. A command that overrides the pair
    passes the name of its own: ``"dev-shell"``, ``"repl"``, or ``"exact"`` for
    a command that applies no search at all.
    """
    return _flake_option(complete=flake_completer(search))
