from __future__ import annotations

import sys
from typing import override

import structlog
from clypi import arg

from pynix._settings import ConfiguredCommand, store_option
from pynix._util import error_exit, eval_session, print_json, report_and_exit
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
    evaluate_target,
    file_option,
    flake_option,
)

logger = structlog.get_logger(__name__)


class Eval(ConfiguredCommand):
    """Evaluate a Nix expression and print the result as JSON"""

    expr: str | None = arg(None, help="Nix expression to evaluate. Reads from stdin if not provided.")
    file: str | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        target = EvaluationTarget.from_command(self)
        try:
            target.validate()
        except EvaluationTargetError as exc:
            report_and_exit(exc)
        if target.file is not None or target.flake is not None:
            if self.expr is not None:
                error_exit("expression argument cannot be combined with --file or --flake")
            expr = None
        elif self.expr is not None:
            expr = self.expr
            logger.info("reading expression from argument")
        else:
            expr = sys.stdin.read()
            logger.info("reading expression from stdin")

        async with eval_session(self.store) as (_nix, _store, session):
            try:
                root = (
                    await evaluate_target(target, session, auto_call_file=True)
                    if expr is None
                    else await session.string(expr)
                )
            except EvaluationTargetError as exc:
                report_and_exit(exc)
            value = await root.to_python()
            print_json(value)
