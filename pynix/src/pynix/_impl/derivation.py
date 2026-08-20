"""The implementation of the ``pynix derivation`` command.

``pynix.derivation`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from pynix._util import print_json, report_and_exit, store_session
from pynix.derivation import Show
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    base_attr_search,
    derivation_path,
    evaluate_target,
)

logger = structlog.get_logger("pynix.derivation")


async def run_show(command: Show) -> None:
    """The body of :meth:`pynix.derivation.Show.run`."""
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)

    async with store_session(command.store) as (nix, store):
        async with nix.eval(store) as session:
            try:
                root = await evaluate_target(target, session, auto_call_file=True, attr_search=base_attr_search())
            except EvaluationTargetError as exc:
                report_and_exit(exc)

            try:
                drv_path = await derivation_path(root, selected=target.selected_attr())
            except EvaluationTargetError as exc:
                report_and_exit(exc)

        derivation = await store.read_derivation(drv_path)
        result = {drv_path: _derivation_to_dict(derivation)}
        print_json(result)


def _input_drv_to_dict(node: Any) -> dict[str, object]:
    """One ``inputDrvs`` node, children included.

    ``dynamic_outputs`` used to be a flat ``dict[str, str]``, so a plain
    ``dict(...)`` was enough to make it JSON-serializable. It is a tree of
    nodes now -- matching Nix's own `nix derivation show` output, which
    nests ``dynamicOutputs`` the same way -- so it has to recurse or the
    dump raises on a dynamic derivation.
    """
    return {
        "dynamicOutputs": {name: _input_drv_to_dict(child) for name, child in node.dynamic_outputs.items()},
        "outputs": list(node.outputs),
    }


def _derivation_to_dict(derivation: Any) -> dict[str, object]:
    result: dict[str, object] = {
        "name": derivation.name,
        "system": derivation.system,
        "builder": derivation.builder,
        "args": list(derivation.args),
        "env": dict(derivation.env),
        "inputDrvs": {k: _input_drv_to_dict(v) for k, v in derivation.input_drvs.items()},
        "inputSrcs": list(derivation.input_srcs),
        "outputs": {
            k: {
                k2: v2
                for k2, v2 in {
                    "path": v.path,
                    "hashAlgo": v.hash_algo,
                    "method": v.method,
                    "ca": v.ca,
                    "type": v.type,
                }.items()
                if v2 is not None
            }
            for k, v in derivation.outputs.items()
        },
    }
    # Key name, shape and omission all follow Nix's own `nix derivation
    # show` (`derivations.cc`'s toJSON: `res["structuredAttrs"] =
    # d.structuredAttrs->structuredAttrs`, guarded by the optional) -- the
    # decoded object rather than the raw payload, and absent entirely for a
    # derivation that does not use structured attrs.
    if derivation.structured_attrs is not None:
        result["structuredAttrs"] = json.loads(derivation.structured_attrs)
    return result
