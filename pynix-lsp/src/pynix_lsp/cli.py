"""Nix language server command."""

from __future__ import annotations

from typing import override

import rich.traceback

from nanopynix import set_manager_title
from pynix._cli import build_parser, complete, dispatch
from pynix._impl.main import run
from pynix._settings import PynixCommand
from pynix._util import configure_logging
from pynix_lsp._handlers import create_server


class PynixLsp(PynixCommand):
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


def main() -> None:
    """Run the server as the `pynix-lsp` program.

    **One name.** An editor configures one command, and `pynix-lsp` is the name
    that every other Nix language server uses. `pynix._cli` reads the name of a
    command from the name of its class, so this class is spelled `PynixLsp` and
    the program is `pynix-lsp`.

    `pynix` used to mount this command as its `lsp` subcommand as well, through
    an optional import. That alias is gone: it cost a subcommand union written
    twice, a meta test to keep the two halves in step, a third question in
    `checks.pynix-isolated`, and a dev shell that loaded 647 modules for
    `import pynix` where a release build loads 202. Issue #107 made the split
    and issue #123 removed the alias.
    """
    parser = build_parser(PynixLsp)
    complete(parser)
    command = dispatch(parser, parser.parse_args())
    rich.traceback.install(show_locals=True)
    set_manager_title("pynix-lsp")
    configure_logging()
    run(command.run)
