from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, override

import structlog
from clypi import Command, arg
from rich.console import Console

import nanopynix
from pynix._util import forward_nix_logs, prepare_sys_path
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
    evaluate_target,
    file_option,
    flake_option,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)
console = Console()

_DEFAULT_STORE = "auto"


class Eval(Command):
    """Evaluate a Nix expression and print the result as JSON"""

    expr: str | None = arg(None, help="Nix expression to evaluate. Reads from stdin if not provided.")
    file: Path | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
    store: str = arg(_DEFAULT_STORE, help="Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        prepare_sys_path()

        target = EvaluationTarget.from_command(self)
        try:
            target.validate()
        except EvaluationTargetError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise SystemExit(1) from exc
        if target.file is not None or target.flake is not None:
            if self.expr is not None:
                console.print("[red]Error:[/red] expression argument cannot be combined with --file or --flake")
                raise SystemExit(1)
            expr = None
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
            try:
                root = await evaluate_target(target, session, auto_call_file=True) if expr is None else await session.string(expr)
            except EvaluationTargetError as exc:
                console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc
            value = await root.force_json()
            result = json.dumps(value, sort_keys=True, indent=2)
            sys.stdout.write(result)
            sys.stdout.write("\n")
