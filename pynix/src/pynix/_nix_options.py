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

``--flake`` gets no completer yet. Its attribute path goes through the search
of ``select_flake_attr`` -- ``packages.<system>``, then
``legacyPackages.<system>``, then the top level -- and a completion has to
offer the union of those three under the name the caller typed. Issue #227
holds it.
"""

from __future__ import annotations

from libpynix import attr_option as _attr_option, file_option as _file_option, flake_option as _flake_option
from nanopynix._typechecking import no_runtime_type_check
from pynix._attr_completion import complete_attr, complete_file


@no_runtime_type_check  # a declaration returns a Spec, not the annotated type; see libpynix.nix_options
def file_option() -> str | None:
    """``--file``, completing an attribute path after a ``#``."""
    return _file_option(complete=complete_file)


@no_runtime_type_check  # see file_option
def attr_option() -> str | None:
    """``--attr``, completing against the ``--file`` beside it."""
    return _attr_option(complete=complete_attr)


@no_runtime_type_check  # see file_option
def flake_option() -> str | None:
    """``--flake``, with no completer. See the note in the module docstring."""
    return _flake_option()
