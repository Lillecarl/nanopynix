r"""Take the ANSI escape sequences out of the text that Nix wrote.

Nix writes the escape sequences that reach this library, so Nix owns the
answer to which bytes are an escape sequence. ``nix::filterANSIEscapes`` is
that answer, and :func:`strip_ansi` is a call to it.

This replaced the ``strip-ansi`` package, whose whole implementation is the
pattern ``\x1B\[\d+(;\d+){0,2}m``. That pattern reads one shape of one
sequence: an SGR sequence of at most three numeric parameters. It covers the
macros in Nix's ``ansicolor.hh`` that Nix uses today, and it leaves these
three behind. Each one is measured, and each one is a test in
``tests/nanopynix/bindings/test_util_bindings.py``:

- An OSC 8 hyperlink, ``\x1b]8;;http://x\x1b\a\x1b]8;;\x1b\``. The pattern
  returns the whole input unchanged. Nix returns ``a``, and Nix has a test of
  its own for both terminators (``src/libutil-tests/terminal.cc``).
- A 24-bit colour, ``\x1b[38;2;255;0;0mred\x1b[0m``. Five parameters, so the
  pattern matches only the reset and returns ``\x1b[38;2;255;0;0mred``.
- A sequence that does not end in ``m``, such as ``\x1b[2K``.

None of the three needs a change in this repository to appear. Each one needs
a change in Nix, or an error that Nix reports from a library it calls.

**Two differences go the other way, and both are Nix's behaviour rather than
a defect.** A tab becomes spaces to the next multiple of eight, and a
carriage return and a bell go. The pattern kept all three.

The function reads no configuration and touches no global state, so it needs
no ``init_libstore`` and it runs on any thread. That is what lets it sit in a
pydantic validator and in an exception accessor, which run wherever the
caller happens to be.
"""

from __future__ import annotations

from nanopynix_bindings.util import filter_ansi_escapes


def strip_ansi(text: str) -> str:
    """Return *text* with every ANSI escape sequence removed."""
    return filter_ansi_escapes(text, filter_all=True)
