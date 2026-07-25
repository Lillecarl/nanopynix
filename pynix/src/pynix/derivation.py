from __future__ import annotations

from pathlib import Path  # noqa: TC003 -- clypi evaluates annotations at runtime, Path must be importable
from typing import TYPE_CHECKING, Any, override

import structlog
from clypi import Command, arg

from nanopynix import NixType

if TYPE_CHECKING:
    from nanopynix.rpc import ValueProxy

from pynix._util import error_exit, print_json, report_and_exit, store_session
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
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

                drv_path = await self._get_drv_path(root)

            derivation = await store.read_derivation(drv_path)
            result = {drv_path: self._derivation_to_dict(derivation)}
            print_json(result)

    @staticmethod
    async def _get_drv_path(value: ValueProxy) -> str:
        # has_attr() is an attrset question, so ask whether this is an attrset
        # first. It used to answer False for any non-attrset, which made
        # "is this a derivation?" work by accident on a string or a list; it
        # now raises, which is right for an accessor but means the type test
        # has to be explicit.
        if await value.get_type() != NixType.ATTRS or not await value.has_attr("type"):
            error_exit("value is not a derivation")
        value_type = await value.attr("type").to_python()
        if value_type != "derivation":
            error_exit(f"value at attribute path is not a derivation (got {value_type!r})")
        drv_path = await value.attr("drvPath").to_python()
        if not isinstance(drv_path, str):
            error_exit("failed to get derivation path")
        return drv_path

    @staticmethod
    def _derivation_to_dict(derivation: Any) -> dict[str, object]:
        return {
            "name": derivation.name,
            "system": derivation.system,
            "builder": derivation.builder,
            "args": list(derivation.args),
            "env": dict(derivation.env),
            "inputDrvs": {
                k: {
                    "dynamicOutputs": dict(v.dynamic_outputs),
                    "outputs": list(v.outputs),
                }
                for k, v in derivation.input_drvs.items()
            },
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


class Derivation(Command):
    """Inspect and manipulate Nix derivations"""

    subcommand: Show
