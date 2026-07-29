from __future__ import annotations

import difflib

# A real import, not a TYPE_CHECKING one: clypi resolves the annotations on
# the command below at runtime to build its argument parser, so `Path` has to
# exist as an object and not just as a lazy PEP 563 string.
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import structlog
from clypi import Command, arg
from nanopynix_helpers.build import FodBuildError, build_with_fod_update

import nanopynix
from nanopynix._typechecking import BEARTYPING
from pynix._util import error_console, error_exit, nix_session, print_json, report_and_exit

if TYPE_CHECKING or BEARTYPING:
    from nanopynix.rpc import ValueProxy
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
    evaluate_target,
    file_option,
    flake_option,
)

logger = structlog.get_logger("pynix.build")

_DEFAULT_SUBSTITUTERS = "https://cache.nixos.org/"
_DEFAULT_TRUSTED_PUBLIC_KEYS = "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="


class Build(Command):
    """Build a Nix derivation value"""

    file: Path | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
    store: str = arg("auto", help="Store URI to build with.")
    eval_store: str | None = arg(None, help="Store URI to evaluate with. Defaults to --store.")
    substituters: str = arg(_DEFAULT_SUBSTITUTERS, help="Space-separated substituter URLs.")
    trusted_public_keys: str = arg(_DEFAULT_TRUSTED_PUBLIC_KEYS, help="Space-separated substituter public keys.")
    # None, not a literal level: nanopynix leaves Nix's own compiled-in default
    # (info) alone when no verbosity is named, and pynix has no reason to
    # disagree with the library it dogfoods. This used to force notice, so
    # `pynix build` was quieter than every other consumer of the same session.
    verbosity: str | None = arg(
        None,
        help="Nix log verbosity: error, warn, notice, info, talkative, chatty, debug, vomit, or 0-7.",
    )
    print_build_logs: bool = arg(False, help="Print build log lines to stderr.")
    update_fod: bool = arg(False, help="Update plain fixed-output hash literals after a hash mismatch.")
    dry_run: bool = arg(False, help="Show --update-fod changes without writing or rebuilding.")

    @override
    async def run(self) -> None:
        target = EvaluationTarget.from_command(self)
        try:
            target.validate(required=True)
        except EvaluationTargetError as exc:
            report_and_exit(exc)
        if self.update_fod and target.file is None:
            error_exit("--update-fod currently requires --file")
        if self.dry_run and not self.update_fod:
            error_exit("--dry-run requires --update-fod")

        settings = nanopynix.NixSettingsEnv(
            substituters=self.substituters.split(),
            trusted_public_keys=self.trusted_public_keys.split(),
        )
        async with nix_session(
            settings=settings,
            verbosity=self.verbosity,
            print_build_logs=self.print_build_logs,
        ) as nix:
            try:
                if self.eval_store is None:
                    async with nix.store(self.store) as store:
                        async with nix.eval(store) as session:
                            logger.info("pynix build evaluating target")
                            outputs, updates = await _build_target(
                                target,
                                session,
                                nix=nix,
                                evaluation_store=store,
                                update_fod=self.update_fod,
                                dry_run=self.dry_run,
                            )
                        logger.info("pynix build finished")
                else:
                    async with (
                        nix.store(self.eval_store) as eval_store,
                        nix.store(self.store) as build_store,
                    ):
                        async with nix.eval(eval_store) as session:
                            logger.info("pynix build evaluating target")
                            outputs, updates = await _build_target(
                                target,
                                session,
                                nix=nix,
                                evaluation_store=eval_store,
                                build_store=build_store,
                                update_fod=self.update_fod,
                                dry_run=self.dry_run,
                            )
                        logger.info("pynix build finished")
            except BuildTargetError as exc:
                report_and_exit(exc)

        print_json({"outputs": outputs, "updatedFods": updates, "dryRun": self.dry_run})


async def _evaluate_build_target(target: EvaluationTarget, session: Any) -> ValueProxy:
    try:
        return await evaluate_target(target, session, auto_call_file=True)
    except EvaluationTargetError as exc:
        raise BuildTargetError(str(exc)) from exc


class BuildTargetError(EvaluationTargetError):
    pass


async def _build_target(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    target: EvaluationTarget,
    session: Any,
    *,
    nix: Any,
    evaluation_store: Any,
    build_store: Any = None,
    update_fod: bool,
    dry_run: bool,
) -> tuple[dict[str, str], int]:
    """Build a target, applying only unambiguous plain-string FOD updates."""

    async def _evaluate() -> ValueProxy:
        root = await _evaluate_build_target(target, session)
        logger.info("pynix build target evaluated")
        return root

    try:
        return await build_with_fod_update(
            _evaluate,
            nix=nix,
            eval_session=session,
            evaluation_store=evaluation_store,
            build_store=build_store,
            update_fod=update_fod,
            source_file=target.file,
            dry_run=dry_run,
            on_hash_update=_print_diff,
        )
    except FodBuildError as exc:
        raise BuildTargetError(str(exc)) from exc


def _print_diff(path: Path, before: str, after: str) -> None:
    error_console.print(
        "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
            ),
        ),
        markup=False,
        highlight=False,
        end="",
    )
