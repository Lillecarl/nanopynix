"""The implementation of the ``pynix config`` command.

``pynix.config`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import nanopynix
from pynix._util import print_json
from pynix.config import Check, CurrentSystem, Show


async def run_show(command: Show) -> None:
    """The body of :meth:`pynix.config.Show.run`."""
    nanopynix.init_libstore(load_config=True)
    settings = nanopynix.list_settings()
    if command.setting is not None:
        # Read one out of the whole registry rather than asking Nix for the
        # single name. A per-setting getter on the module reports the
        # globals of *this* process, which is the wrong process as soon as
        # a worker holds Nix, so nanopynix no longer offers one.
        print_json({command.setting: settings.get(command.setting)})
        return
    print_json(settings)


async def run_check(command: Check) -> None:  # noqa: ARG001 -- every runner takes its command, so the shape does not depend on which options one reads
    """The body of :meth:`pynix.config.Check.run`."""
    nanopynix.init_libstore(load_config=True)
    print_json({"ok": True})


async def run_current_system(command: CurrentSystem) -> None:  # noqa: ARG001 -- see run_check
    """The body of :meth:`pynix.config.CurrentSystem.run`."""
    nanopynix.init_libstore(load_config=True)
    print_json({"currentSystem": nanopynix.current_system()})
