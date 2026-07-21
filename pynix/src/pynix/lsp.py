"""Nix language server command."""

from __future__ import annotations

from typing import override

from clypi import Command

from pynix._lsp._handlers import create_server


class Lsp(Command):
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
