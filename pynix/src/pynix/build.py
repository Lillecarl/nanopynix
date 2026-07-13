from __future__ import annotations

import json
import sys
from pathlib import Path  # noqa: TC003
from typing import override

import structlog
from clypi import Command, arg
from rich.console import Console

from pynix._util import forward_nix_logs, prepare_sys_path

console = Console()
error_console = Console(stderr=True)
logger = structlog.get_logger("pynix.build")

_DEFAULT_SUBSTITUTERS = "https://cache.nixos.org/"
_DEFAULT_TRUSTED_PUBLIC_KEYS = "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
_DEFAULT_VERBOSITY = "notice"


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
    eval_store: str | None = arg(None, help="Store URI to evaluate with. Defaults to --store.")
    substituters: str = arg(_DEFAULT_SUBSTITUTERS, help="Space-separated substituter URLs.")
    trusted_public_keys: str = arg(_DEFAULT_TRUSTED_PUBLIC_KEYS, help="Space-separated substituter public keys.")
    verbosity: str = arg(
        _DEFAULT_VERBOSITY,
        help="Nix log verbosity: error, warn, notice, info, talkative, chatty, debug, vomit, or 0-7.",
    )
    print_build_logs: bool = arg(False, help="Print build log lines to stderr.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        if self.file is None and self.flake is None:
            error_console.print("[red]Error:[/red] either --file or --flake is required")
            raise SystemExit(1)
        if self.file is not None and self.flake is not None:
            error_console.print("[red]Error:[/red] --file and --flake are mutually exclusive")
            raise SystemExit(1)

        settings = nanopynix.NixSettingsEnv(
            substituters=self.substituters.split(),
            trusted_public_keys=self.trusted_public_keys.split(),
        )
        async with (
            nanopynix.Session(settings=settings, verbosity=nanopynix.normalize_log_level(self.verbosity)) as nix,
            forward_nix_logs(nix, print_build_logs=self.print_build_logs),
        ):
            try:
                if self.eval_store is None:
                    async with nix.store(self.store) as store:
                        async with nix.eval(store) as session:
                            logger.info("pynix build evaluating target")
                            root = await _evaluate_build_target(self, session, attrs_type=nanopynix.NixType.ATTRS)
                            logger.info("pynix build target evaluated")
                            outputs = await root.build()
                        logger.info("pynix build finished")
                else:
                    async with (
                        nix.store(self.eval_store) as eval_store,
                        nix.store(self.store) as build_store,
                    ):
                        async with nix.eval(eval_store) as session:
                            logger.info("pynix build evaluating target")
                            root = await _evaluate_build_target(self, session, attrs_type=nanopynix.NixType.ATTRS)
                            logger.info("pynix build target evaluated")
                            outputs = await root.build(store=build_store)
                        logger.info("pynix build finished")
            except BuildTargetError as exc:
                error_console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc

        _print_json({"outputs": outputs})


class BuildTargetError(RuntimeError):
    pass


async def _navigate(root, attrpath: str, *, attrs_type):
    for part in attrpath.split("."):
        actual = await root.get_type()
        if actual != attrs_type:
            raise BuildTargetError(f"cannot select attribute {part!r} from {actual.name.lower()} value")
        if not await root.has_attr(part):
            names = await root.attr_names()
            available = ", ".join(names[:10])
            suffix = "" if len(names) <= 10 else f", ... ({len(names)} total)"
            raise BuildTargetError(f"attribute {part!r} not found; available attributes: {available}{suffix}")
        root = root.attr(part)
    return root


async def _evaluate_build_target(command: Build, session, *, attrs_type):
    if command.file is not None:
        root = await (await session.file(str(command.file))).auto_call()
        if command.attrpath is not None:
            root = await _navigate(root, command.attrpath, attrs_type=attrs_type)
        return root
    if command.flake is None:
        error_console.print("[red]Error:[/red] either --file or --flake is required")
        raise SystemExit(1)
    base_ref, _, flake_attr = command.flake.partition("#")
    root = await session.eval_flake(base_ref)
    if flake_attr:
        root = await _navigate(root, flake_attr, attrs_type=attrs_type)
    return root


def _print_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")
