from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import structlog
from clypi import Command, arg
from rich.console import Console

if TYPE_CHECKING:
    from nanopynix._session import ValueProxy

from pynix._util import forward_nix_logs, prepare_sys_path
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
    evaluate_target,
    file_option,
    flake_option,
)

console = Console()
error_console = Console(stderr=True)
logger = structlog.get_logger("pynix.build")

_DEFAULT_SUBSTITUTERS = "https://cache.nixos.org/"
_DEFAULT_TRUSTED_PUBLIC_KEYS = "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
_DEFAULT_VERBOSITY = "notice"


class Build(Command):
    """Build a Nix derivation value"""

    file: Path | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
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

        target = EvaluationTarget.from_command(self)
        try:
            target.validate(required=True)
        except EvaluationTargetError as exc:
            error_console.print(f"[red]Error:[/red] {exc}")
            raise SystemExit(1) from exc

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
                            root = await _evaluate_build_target(target, session)
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
                            root = await _evaluate_build_target(target, session)
                            logger.info("pynix build target evaluated")
                            outputs = await root.build(store=build_store)
                        logger.info("pynix build finished")
            except BuildTargetError as exc:
                error_console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc

        _print_json({"outputs": outputs})


async def _evaluate_build_target(target: EvaluationTarget, session: Any) -> ValueProxy:
    try:
        return await evaluate_target(target, session, auto_call_file=True)
    except EvaluationTargetError as exc:
        raise BuildTargetError(str(exc)) from exc


class BuildTargetError(EvaluationTargetError):
    pass


def _print_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")
