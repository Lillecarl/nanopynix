from __future__ import annotations

import json
import sys
from pathlib import Path  # noqa: TC003 -- clypi evaluates annotations at runtime, Path must be importable
from typing import TYPE_CHECKING, Any, override

import structlog
from clypi import Command, arg
from rich.console import Console

if TYPE_CHECKING:
    from nanopynix.rpc.client import ValueProxy

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

logger = structlog.get_logger(__name__)
console = Console()

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
        prepare_sys_path()

        target = EvaluationTarget.from_command(self)
        try:
            target.validate(required=True)
        except EvaluationTargetError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise SystemExit(1) from exc

        async with (
            nanopynix.Session(experimental_features=["flakes", "nix-command"]) as nix,
            forward_nix_logs(nix),
            nix.store(self.store) as store,
        ):
            async with nix.eval(store) as session:
                try:
                    root = await evaluate_target(target, session, auto_call_file=True)
                except EvaluationTargetError as exc:
                    console.print(f"[red]Error:[/red] {exc}")
                    raise SystemExit(1) from exc

                drv_path = await self._get_drv_path(root)

            derivation = await store.read_derivation(drv_path)
            result = {drv_path: self._derivation_to_dict(derivation)}
            sys.stdout.write(json.dumps(result, sort_keys=True, indent=2))
            sys.stdout.write("\n")

    @staticmethod
    async def _get_drv_path(value: ValueProxy) -> str:
        if not await value.has_attr("type"):
            console.print("[red]Error:[/red] value is not a derivation")
            raise SystemExit(1)
        value_type = await value.attr("type").force_json()
        if value_type != "derivation":
            console.print(f"[red]Error:[/red] value at attribute path is not a derivation (got {value_type!r})")
            raise SystemExit(1)
        drv_path = await value.attr("drvPath").force_json()
        if not isinstance(drv_path, str):
            console.print("[red]Error:[/red] failed to get derivation path")
            raise SystemExit(1)
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
