"""Nix language server command."""

from __future__ import annotations

from typing import override

import rich.traceback

from nanopynix import set_manager_title
from pynix._settings import PynixCommand
from pynix._util import configure_logging
from pynix_lsp._handlers import create_server


class Lsp(PynixCommand):
    """Run pynix as a Nix language server (stdio transport).

    Files opt in to real hover/completion by naming a bound identifier and a
    Nix expression to evaluate in a header comment near the top of the file:

        # pynix-lsp: cfg = (import ./flake.nix).nixosConfigurations.myhost.config.services.foo

    Any attribute path in the file rooted at that name (e.g. ``cfg.enable``)
    is then resolved through the expression's evaluated value.
    """

    @override
    async def run(self) -> None:
        await create_server().start_io_async()


# The same command, under the name that the standalone program uses. clypi
# reads the name of a command from the name of its class (`Command.prog`), so
# `Lsp` is `lsp` and this subclass is `pynix-lsp`. Two classes, because both
# names are correct and neither one can take the other position: `pynix
# pynix-lsp` and a program called `lsp` are each wrong.
#
# `__doc__` is taken from `Lsp` rather than written again, because clypi turns
# it into the help text and one description of one command must not drift.
class PynixLsp(Lsp):
    __doc__ = Lsp.__doc__


def main() -> None:
    """Run the server as the `pynix-lsp` program.

    **The server has two names, and each one has a reason.** An editor
    configures one command, so this project installs `pynix-lsp`, which is the
    name that every other Nix language server uses. `pynix` also mounts `Lsp`
    as its `lsp` subcommand when this project is installed beside it, which is
    what a developer in the shell of this repository calls. The release build
    of `pynix` does not carry this project, so `pynix lsp` is not a subcommand
    there. Issue #107 gives the measurement that made the split.
    """
    rich.traceback.install(show_locals=True)
    set_manager_title("pynix-lsp")
    configure_logging()
    PynixLsp.parse().start()
