"""Static and dynamic shell completion for a cyclopts program.

cyclopts generates a static completion script for fish, bash and zsh. It has no
dynamic completion at all: ``cyclopts.completion.CompletionAction`` offers
``NONE``, ``FILES`` and ``DIRECTORIES``, and nothing that calls back into the
program. Upstream issue #641 asks for such a hook, and no work on it started.

This package answers the question that decides the shape of the `pynix` work:
can a dynamic candidate be added to a cyclopts-generated script **without
editing that script**? `_layer` holds the answer, one shape for each shell.
`_pty` drives a real shell to prove it.

**`_pty` is deliberately absent from this file.** It imports `pexpect`, which
is a test dependency and not a runtime one, so re-exporting it from here made
the installed `demo` fail to start: `pexpect` is a `nativeCheckInput` of the
Nix build and is not in the runtime closure. A test imports
`completion_spike._pty` by its own name.
"""

from __future__ import annotations

from completion_spike._layer import (
    DynamicValue as DynamicValue,
    entry_point as entry_point,
    render_layer as render_layer,
    render_script as render_script,
)
from completion_spike._line import Line as Line, read_line as read_line
