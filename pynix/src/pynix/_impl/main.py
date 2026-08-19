"""What ``pynix`` sets up once clypi has decided that a command runs.

**Nothing here is needed to print help or to answer a completion.**
``main`` used to call all three of these before ``Pynix.parse()``, so a
completion callback configured a logger, installed a traceback handler and
set a process title, and then exited inside ``parse``. ``configure_logging``
alone pulled ``structlog``, which is 195 ms and brings ``rich``, ``asyncio``
and ``attr`` with it. Issue #123.

``parse`` now runs first. A usage error still reads well, because clypi
formats those itself. A genuine defect inside ``parse`` prints a plain
traceback rather than a rich one, and that is the whole of the cost.
"""

from __future__ import annotations

import rich.traceback

from nanopynix import set_manager_title
from pynix._util import configure_logging


def prepare() -> None:
    """Install the traceback handler, name the process and configure logging."""
    rich.traceback.install(show_locals=True)
    set_manager_title("pynix")
    configure_logging()
