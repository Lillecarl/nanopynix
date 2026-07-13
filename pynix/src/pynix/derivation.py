from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003
from typing import override

import structlog
from clypi import Command, arg
from nanopynix_proto.nix.store import ReadDerivationRequest
from rich.console import Console

from pynix._util import prepare_sys_path

logger = structlog.get_logger(__name__)
console = Console()


class Show(Command):
    """Show the contents of a Nix derivation

    Examples:
      pynix derivation show --file default.nix --attrpath hello
      pynix derivation show --flake .#hello
      pynix derivation show --flake nixpkgs#python3Packages.requests"""

    file: Path | None = arg(
        None,
        short="f",
        help="Evaluate FILE.  Use --attrpath to select a derivation inside it.",
    )
    attrpath: str | None = arg(
        None,
        short="A",
        help="Dot-separated attribute path to the derivation (e.g. 'python3Packages.requests').",
    )
    flake: str | None = arg(
        None,
        help="Flake reference (e.g. '.#hello').  The attrpath after '#' selects the derivation.",
    )

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        if self.file is None and self.flake is None:
            console.print("[red]Error:[/red] either --file or --flake is required")
            raise SystemExit(1)
        if self.file is not None and self.flake is not None:
            console.print("[red]Error:[/red] --file and --flake are mutually exclusive")
            raise SystemExit(1)

        async with nanopynix.Session(experimental_features=["flakes", "nix-command"]) as nix, nix.store() as store:
            async with nix.eval(store) as session:
                if self.file is not None:
                    root = await session.file(str(self.file))
                    if self.attrpath is not None:
                        root = self._navigate(root, self.attrpath)
                else:
                    assert self.flake is not None
                    base_ref, _, flake_attr = self.flake.partition("#")
                    root = await session.eval_flake(base_ref)
                    if flake_attr:
                        root = self._navigate(root, flake_attr)

                drv_path = await self._get_drv_path(root)

            derivation = await store.read_derivation(ReadDerivationRequest(path=drv_path))
            result = {drv_path: self._derivation_to_dict(derivation)}
            console.print(json.dumps(result, sort_keys=True, indent=2))

    @staticmethod
    def _navigate(root, attrpath: str):
        for part in attrpath.split("."):
            root = root.attr(part)
        return root

    @staticmethod
    async def _get_drv_path(value) -> str:
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
    def _derivation_to_dict(derivation) -> dict[str, object]:
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
