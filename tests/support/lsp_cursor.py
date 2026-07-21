"""Shared cursor-position arithmetic for LSP tests.

Both the in-process handler tests (``tests/pynix/test_lsp.py``) and the
real-subprocess protocol tests (``tests/pynix/test_lsp_e2e.py``) need to turn
"the position just after this bit of text in a fixture" into an LSP
``Position``. Centralizing that here means it's implemented (and tested)
once, instead of as a dozen near-identical ``lines``/``index`` blocks spread
across test files.
"""

from __future__ import annotations

from lsprotocol import types


def cursor_after(source: str, target: str, *, needle: str | None = None, offset: int = 1) -> types.Position:
    """Return the ``Position`` ``offset`` characters into ``target``'s first occurrence.

    If ``needle`` is given, the search for ``target`` is narrowed to the
    first line containing ``needle`` first -- for fixtures where ``target``
    itself appears more than once (e.g. ``pkgs`` referenced on several
    lines, but only one of them is the one under test).
    """
    lines = source.splitlines()
    line_index = next(i for i, line in enumerate(lines) if (needle or target) in line)
    character = lines[line_index].index(target) + offset
    return types.Position(line=line_index, character=character)
