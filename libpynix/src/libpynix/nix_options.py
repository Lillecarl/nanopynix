"""The three options that name what to evaluate, for any Nix CLI.

``--file``, ``--flake`` and ``--attr`` mean the same thing in every program
that evaluates Nix, and two programs in this organisation had declared them
separately: ``pynix._settings`` and ``easykubenix``'s ``NixCommand``. The help
of ``--attr`` was the same string in both, character for character. Issue #222
is why there is one copy now.

**This module declares the three, and reads none of them.** That split is the
whole reason it can be here. Reading a target means an evaluator, and issue
#123 measured what an evaluator costs a start that evaluates nothing: 101 ms,
because ``pynix.target`` pulls ``structlog`` and the exception tree of
nanopynix. A command module imports whatever declares its options, so a
declaration next to the code that reads it puts that cost on every command.
The code that reads these three stays in the program: ``pynix.target``
resolves them, and ``nanopynix_helpers.eval_target`` holds the search.

**No completer is set.** ``Spec.complete`` exists, and a Tab after any of these
three offers file names, which is what the shell does when nothing answers. An
``--attr`` completer would evaluate Nix on a keypress, so it needs a budget and
a way to give up; issue #223 holds that question.
"""

from __future__ import annotations

from libpynix._typecheck import no_runtime_type_check
from libpynix.command import opt


@no_runtime_type_check  # a declaration returns a Spec, not the annotated type; beartype would otherwise flag every call as a type violation
def file_option() -> str | None:
    """Declare the common ``--file`` option.

    The value is a string, and not a ``Path``. ``PurePath`` collapses a
    repeated separator, so ``https://example.com/x.tar.gz`` reached the
    evaluator as ``https:/example.com/x.tar.gz`` and failed. A reference is
    also not a path: ``github:NixOS/nixpkgs`` and ``<nixpkgs>`` name a tree
    that no local directory holds.
    """
    return opt(
        None,
        short="f",
        help="Evaluate FILE as a Nix expression. FILE is a path, a lookup path, a URL, or a flake reference, and it may end with '#' and an attribute path.",
    )


@no_runtime_type_check  # see file_option
def attr_option() -> str | None:
    """Declare the common ``--attr`` option."""
    return opt(None, short="A", help="Dot-separated attribute path within the evaluation result.")


@no_runtime_type_check  # see file_option
def flake_option() -> str | None:
    """Declare the common ``--flake`` option."""
    return opt(None, help="Evaluate FLAKE, optionally with a '#'-separated attribute path.")
