"""The ``pynix repl`` command.

This module holds the command class and its options, and no more.
``pynix._impl.repl`` holds the prompt, the commands and the session, and
``pynix._impl`` says why the two are apart: the parser loads every subcommand
module on every start, and the REPL's own dependencies were 91.8 ms of that.
"""

from __future__ import annotations

from typing import override

from pynix import _impl
from pynix._settings import ConfiguredCommand, attr_option, file_option, flake_option, store_option


class Repl(ConfiguredCommand):
    """Open an interactive Nix evaluation session."""

    store: str = store_option("Store URI to evaluate with.")
    file: str | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()

    @override
    async def run(self) -> None:
        await _impl.repl.run_repl(self)
