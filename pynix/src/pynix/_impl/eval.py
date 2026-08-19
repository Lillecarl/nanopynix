"""The implementation of the ``pynix eval`` command.

``pynix.eval`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import sys

import structlog

from pynix._util import error_exit, eval_session, print_json, report_and_exit
from pynix.eval import Eval
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    base_attr_search,
    evaluate_target,
)

logger = structlog.get_logger("pynix.eval")


async def run_eval(command: Eval) -> None:
    """The body of :meth:`pynix.eval.Eval.run`."""
    target = EvaluationTarget.from_command(command)
    try:
        target.validate()
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    if target.file is not None or target.flake is not None:
        if command.expr is not None:
            error_exit("expression argument cannot be combined with --file or --flake")
        expr = None
    elif command.expr is not None:
        expr = command.expr
        logger.info("reading expression from argument")
    else:
        expr = sys.stdin.read()
        logger.info("reading expression from stdin")

    async with eval_session(command.store) as (_nix, _store, session):
        try:
            root = (
                await evaluate_target(target, session, auto_call_file=True, attr_search=base_attr_search())
                if expr is None
                else await session.string(expr)
            )
        except EvaluationTargetError as exc:
            report_and_exit(exc)
        value = await root.to_python()
        print_json(value)
