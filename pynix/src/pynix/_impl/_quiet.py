"""Hold back the terminal while a full-screen interface owns the screen.

**A full-screen application draws the whole terminal, so nothing else may
write to it.** One line on stderr from anywhere lands in the middle of the
drawing, and the screen stays wrong until the next full redraw. This is not a
theoretical risk for ``pynix search``: the detail pane opens a Nix evaluator
while the interface is up, and ``pynix._util.forward_nix_logs`` prints one
structlog line for each log event that the evaluator sends.

:func:`quiet_terminal` catches both halves of that, and prints what it caught
after the interface closes, so nothing is lost:

- ``sys.stderr`` is a buffer for the duration. Every Python writer follows it,
  because ``configure_logging`` reads ``sys.stderr`` when it builds the logger
  and ``rich`` reads it on each write.
- File descriptor 2 goes to a temporary file for the duration, which catches
  a writer that holds the descriptor and not the Python object.

**File descriptor 1 is untouched, and it has to be.** ``prompt_toolkit``
draws to stdout, so redirecting that one leaves the interface with nowhere to
draw.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING

# `or BEARTYPING`, and not `TYPE_CHECKING` alone: beartype resolves the return
# annotation at run time, and a name that only the type checker imported is not
# there to resolve. It gave up on `quiet_terminal` in silence without this, so
# the function had no runtime check at all.
if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Generator

#: The file descriptor of stderr.
_STDERR = 2


@contextlib.contextmanager
def quiet_terminal() -> Generator[None]:
    """Send stderr to a buffer, and print the buffer when the block ends.

    A temporary file holds what the descriptor caught, rather than a pipe: a
    pipe holds 64 KiB and then blocks its writer forever, and the reader of
    this one only runs after the block.
    """
    held = io.StringIO()
    spilled = ""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as spill:
        saved = os.dup(_STDERR)
        try:
            os.dup2(spill.fileno(), _STDERR)
            with contextlib.redirect_stderr(held):
                yield
        finally:
            os.dup2(saved, _STDERR)
            os.close(saved)
            spill.seek(0)
            spilled = spill.read()
    text = held.getvalue() + spilled
    if text.strip():
        sys.stderr.write(text if text.endswith("\n") else f"{text}\n")
