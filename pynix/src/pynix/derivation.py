from __future__ import annotations

import json

# A real import, not a TYPE_CHECKING one: clypi resolves the annotations on
# the command below at runtime to build its argument parser, so `Path` has to
# exist as an object and not just as a lazy PEP 563 string.
from pathlib import Path
from typing import Any, override

import structlog
from clypi import Command, arg

from pynix._util import print_json, report_and_exit, store_session
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
    derivation_path,
    evaluate_target,
    file_option,
    flake_option,
)

logger = structlog.get_logger(__name__)

_DEFAULT_STORE = "auto"


class Show(Command):
    """Show the contents of a Nix derivation

    Examples:
      pynix derivation show --file default.nix --attr hello
      pynix derivation show --flake .#hello
      pynix derivation show --flake nixpkgs#python3Packages.requests"""

    file: Path | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        target = EvaluationTarget.from_command(self)
        try:
            target.validate(required=True)
        except EvaluationTargetError as exc:
            report_and_exit(exc)

        async with store_session(self.store) as (nix, store):
            async with nix.eval(store) as session:
                try:
                    root = await evaluate_target(target, session, auto_call_file=True)
                except EvaluationTargetError as exc:
                    report_and_exit(exc)

                try:
                    drv_path = await derivation_path(root)
                except EvaluationTargetError as exc:
                    report_and_exit(exc)

            derivation = await store.read_derivation(drv_path)
            result = {drv_path: self._derivation_to_dict(derivation)}
            print_json(result)

    @staticmethod
    def _input_drv_to_dict(node: Any) -> dict[str, object]:
        """One ``inputDrvs`` node, children included.

        ``dynamic_outputs`` used to be a flat ``dict[str, str]``, so a plain
        ``dict(...)`` was enough to make it JSON-serializable. It is a tree of
        nodes now -- matching Nix's own `nix derivation show` output, which
        nests ``dynamicOutputs`` the same way -- so it has to recurse or the
        dump raises on a dynamic derivation.
        """
        return {
            "dynamicOutputs": {name: Show._input_drv_to_dict(child) for name, child in node.dynamic_outputs.items()},
            "outputs": list(node.outputs),
        }

    @staticmethod
    def _derivation_to_dict(derivation: Any) -> dict[str, object]:
        result: dict[str, object] = {
            "name": derivation.name,
            "system": derivation.system,
            "builder": derivation.builder,
            "args": list(derivation.args),
            "env": dict(derivation.env),
            "inputDrvs": {k: Show._input_drv_to_dict(v) for k, v in derivation.input_drvs.items()},
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


class Derivation(Command):
    """Inspect and manipulate Nix derivations"""

    subcommand: Show
