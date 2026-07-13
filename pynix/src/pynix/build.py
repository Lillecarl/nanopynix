from __future__ import annotations

import json
import sys
from pathlib import Path  # noqa: TC003
from typing import override

from clypi import Command, arg
from rich.console import Console

from pynix._util import forward_nix_logs, prepare_sys_path

console = Console()

_DEFAULT_SUBSTITUTERS = "https://cache.nixos.org/"
_DEFAULT_TRUSTED_PUBLIC_KEYS = "cache.nixos.org-1:NCHdD59X431o0gWfe6E3gSxWeQa73p0i1d94vQxFh9s="
_DEFAULT_VERBOSITY = 2


class Build(Command):
    """Build a Nix derivation value"""

    file: Path | None = arg(
        None,
        short="f",
        help="Evaluate FILE. Use --attrpath to select a derivation inside it.",
    )
    attrpath: str | None = arg(
        None,
        short="A",
        help="Dot-separated attribute path to the derivation.",
    )
    flake: str | None = arg(
        None,
        help="Flake reference (e.g. '.#hello'). The attrpath after '#' selects the derivation.",
    )
    store: str = arg("auto", help="Store URI to build with.")
    eval_store: str = arg("auto", help="Store URI to evaluate with.")
    substituters: str = arg(_DEFAULT_SUBSTITUTERS, help="Space-separated substituter URLs.")
    trusted_public_keys: str = arg(_DEFAULT_TRUSTED_PUBLIC_KEYS, help="Space-separated substituter public keys.")
    verbosity: int = arg(_DEFAULT_VERBOSITY, help="Nix log verbosity, 0=error through 7=vomit.")
    print_build_logs: bool = arg(False, help="Print build log lines to stderr.")

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

        settings = {
            "substituters": self.substituters,
            "trusted-public-keys": self.trusted_public_keys,
            "__nanopynix_verbosity": str(self.verbosity),
        }
        async with (
            nanopynix.Session(experimental_features=["flakes", "nix-command"], settings=settings) as nix,
            forward_nix_logs(nix, print_build_logs=self.print_build_logs),
            nix.store(self.eval_store) as eval_store,
            nix.store(self.store) as build_store,
            nix.eval(eval_store) as session,
        ):
            if self.file is not None:
                root = await session.file(str(self.file))
                if self.attrpath is not None:
                    root = _navigate(root, self.attrpath)
            else:
                if self.flake is None:
                    console.print("[red]Error:[/red] either --file or --flake is required")
                    raise SystemExit(1)
                base_ref, _, flake_attr = self.flake.partition("#")
                root = await session.eval_flake(base_ref)
                if flake_attr:
                    root = _navigate(root, flake_attr)

            outputs = await root.build(store=build_store)

        _print_json({"outputs": outputs})


def _navigate(root, attrpath: str):
    for part in attrpath.split("."):
        root = root.attr(part)
    return root


def _print_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")
