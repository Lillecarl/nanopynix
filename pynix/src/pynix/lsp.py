"""Nix language server command."""

from __future__ import annotations

from typing import override

import anyio.to_thread
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
        # pygls' start_io() is synchronous and owns its own asyncio.run(...)
        # internally, which cannot nest inside the loop clypi already runs
        # this command under. Running it on a worker thread avoids that
        # clash; anyio's asyncio backend has no trouble hosting the nanopynix
        # eval sessions our handlers open from within pygls' own loop there.
        await anyio.to_thread.run_sync(create_server().start_io)
