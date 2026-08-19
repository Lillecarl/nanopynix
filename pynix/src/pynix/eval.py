from __future__ import annotations

from typing import override

from pynix import _impl
from pynix._cli import opt
from pynix._settings import ConfiguredCommand, attr_option, file_option, flake_option, store_option


class Eval(ConfiguredCommand):
    """Evaluate a Nix expression and print the result as JSON"""

    expr: str | None = opt(None, help="Nix expression to evaluate. Reads from stdin if not provided.")

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _impl.eval.run_eval(self)
