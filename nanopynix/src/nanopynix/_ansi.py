r"""Take the ANSI escape sequences out of the text that Nix wrote.

Nix writes the escape sequences that reach this library, so Nix owns the
answer to which bytes are an escape sequence. ``nix::filterANSIEscapes`` is
that answer, and :func:`strip_ansi` is a call to it.

This replaced the ``strip-ansi`` package, whose whole implementation is the
pattern ``\x1B\[\d+(;\d+){0,2}m``. That pattern reads one shape of one
sequence: an SGR sequence of at most three numeric parameters. It covers the
macros in Nix's ``ansicolor.hh`` that Nix uses today, and it leaves these
three behind. Each one is measured, and each one is a test in
``nanopynix/tests/bindings/test_util_bindings.py``:

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
    r"""Return *text* with every ANSI escape sequence removed.

    This is ``nix::filterANSIEscapes`` with ``filter_all`` on, so it reads the
    same bytes as an escape sequence that Nix writes. That includes an OSC 8
    hyperlink and a sequence that does not end in ``m``, and no pattern in
    this repository read either one.

    **The function does three more things to the text, and each one is Nix's
    behaviour.** A tab becomes spaces to the next multiple of eight. A
    carriage return goes, and a bell goes. So the result is the text that Nix
    would print to a terminal that has no colour, and not the input with a
    few bytes deleted. Compare an exact string against the result of this
    function, and not against a hand-written expectation of what was removed.

    The function reads no configuration and touches no global state, so it
    needs no ``init_libstore`` and it runs on any thread.
    """
    return filter_ansi_escapes(text, filter_all=True)
