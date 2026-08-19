"""What ``pynix`` sets up once the parser has decided that a command runs.

**Nothing here is needed to print help or to answer a completion.**
``main`` used to call all three of these before it parsed anything, so a
completion callback configured a logger, installed a traceback handler and
set a process title, and then exited during the parse. ``configure_logging``
alone pulled ``structlog``, which is 195 ms and brings ``rich``, ``asyncio``
and ``attr`` with it. Issue #123.

The parse now runs first. A usage error still reads well, because argparse
formats those itself and writes them to stderr. A genuine defect during the
parse prints a plain traceback rather than a rich one, and that is the whole
of the cost.

**The event loop starts here as well, and nowhere earlier.** Every ``run`` is a
coroutine function, and ``anyio`` is 60 modules that ``import pynix`` must not
load -- ``tests/meta/test_import_budget.py`` states that. This module is
reached through ``pynix._impl``, which imports it when a command runs.
"""

from __future__ import annotations

# A real import, and not a `TYPE_CHECKING` one. `NANOPYNIX_BEARTYPING=1` makes
# beartype resolve every annotation at run time, and a name the type checker
# alone can see becomes a forward reference it cannot import: measured as
# `Forward reference "Callable" unimportable from module "pynix._impl.main"`,
# raised inside `pynix develop` in a subprocess. `collections.abc` is already
# loaded by the interpreter, so this costs nothing.
from collections.abc import Callable, Coroutine

import anyio
import rich.traceback

from nanopynix import set_manager_title
from pynix._util import configure_logging


def prepare() -> None:
    """Install the traceback handler, name the process and configure logging."""
    rich.traceback.install(show_locals=True)
    set_manager_title("pynix")
    configure_logging()


def run(body: Callable[[], Coroutine[object, object, None]]) -> None:
    """Run the body of a command.

    One call, so that the choice of an async backend is written down once.
    clypi owned this call and started asyncio; anyio is what the rest of this
    repository uses, and `AGENTS.md` says why.
    """
    anyio.run(body)
