from __future__ import annotations

import json
import sys
from pathlib import Path  # noqa: TC003
from typing import override

import structlog
from clypi import Command, arg
from rich.console import Console

from pynix._util import forward_nix_logs, prepare_sys_path

logger = structlog.get_logger(__name__)
console = Console()

_DEFAULT_STORE = "auto"


class Eval(Command):
    """Evaluate a Nix expression and print the result as JSON"""

    expr: str | None = arg(None, help="Nix expression to evaluate. Reads from stdin if not provided.")
    file: Path | None = arg(None, help="Read expression from file instead of an argument or stdin.", short="f")
    store: str = arg(_DEFAULT_STORE, help="Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        expr: str
        if self.file is not None:
            expr = self.file.read_text()
            logger.info("reading expression from file", file=str(self.file))
        elif self.expr is not None:
            expr = self.expr
            logger.info("reading expression from argument")
        else:
            expr = sys.stdin.read()
            logger.info("reading expression from stdin")

        async with (
            nanopynix.Session() as nix,
            forward_nix_logs(nix),
            nix.store(self.store) as store,
            nix.eval(store) as session,
        ):
            root = await session.string(expr)
            value = await root.force_json()
            result = json.dumps(value, sort_keys=True, indent=2)
            sys.stdout.write(result)
            sys.stdout.write("\n")
