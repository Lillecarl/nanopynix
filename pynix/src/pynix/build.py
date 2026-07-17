from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path  # noqa: TC003 -- clypi evaluates annotations at runtime, Path must be importable
from typing import TYPE_CHECKING, Any, override

import structlog
from clypi import Command, arg
from rich.console import Console

if TYPE_CHECKING:
    from nanopynix._session import ValueProxy

import nanopynix
from nanopynix.exceptions import StoreError
from pynix._util import forward_nix_logs, prepare_sys_path
from pynix.fod import (
    FodSourceUpdateError,
    derivation_name_from_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    mismatch_is_target_fod,
    replace_fod_hash,
)
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
    update_fod: bool = arg(False, help="Update plain fixed-output hash literals after a hash mismatch.")
    dry_run: bool = arg(False, help="Show --update-fod changes without writing or rebuilding.")

    @override
    async def run(self) -> None:
        prepare_sys_path()

        target = EvaluationTarget.from_command(self)
        try:
            target.validate(required=True)
        except EvaluationTargetError as exc:
            error_console.print(f"[red]Error:[/red] {exc}")
            raise SystemExit(1) from exc
        if self.update_fod and target.file is None:
            error_console.print("[red]Error:[/red] --update-fod currently requires --file")
            raise SystemExit(1)
        if self.dry_run and not self.update_fod:
            error_console.print("[red]Error:[/red] --dry-run requires --update-fod")
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
                error_console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc

        _print_json({"outputs": outputs, "updatedFods": updates, "dryRun": self.dry_run})


async def _evaluate_build_target(target: EvaluationTarget, session: Any) -> ValueProxy:
    try:
        return await evaluate_target(target, session, auto_call_file=True)
    except EvaluationTargetError as exc:
        raise BuildTargetError(str(exc)) from exc


class BuildTargetError(EvaluationTargetError):
    pass


async def _build_target(
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
    updates = 0
    while True:
        root: ValueProxy | None = None
        async with nix.capture_logs() as logs:
            try:
                root = await _evaluate_build_target(target, session)
                logger.info("pynix build target evaluated")
                outputs = await (root.build() if build_store is None else root.build(store=build_store))
            except StoreError as exc:
                mismatch = extract_fod_hash_mismatch(exc.msg)
                # The daemon's build response only contains a generic failure.
                # The exact mismatch is emitted first as a Nix log event.
                if mismatch is None:
                    mismatch = extract_unique_fod_hash_mismatch(
                        event.message_without_ansi for event in logs.events if event.message_without_ansi is not None
                    )
                if not update_fod or mismatch is None:
                    if root is not None:
                        await root.release()
                    raise
                if mismatch.drv_path is not None and (
                    root is None or not await mismatch_is_target_fod(evaluation_store, root, mismatch)
                ):
                    if root is not None:
                        await root.release()
                    raise BuildTargetError(
                        "fixed-output hash mismatch was not found in the evaluated target derivation closure"
                    ) from exc
                if root is not None:
                    await root.release()
                if target.file is None:
                    raise BuildTargetError("--update-fod currently requires --file") from exc
                if updates >= 10:
                    raise BuildTargetError("stopped after 10 fixed-output hash updates") from exc
                try:
                    source = target.file.read_text()
                    literal = find_fod_hash_literal(
                        source,
                        mismatch.specified,
                        derivation_name=derivation_name_from_path(mismatch.drv_path),
                    )
                    updated = replace_fod_hash(source, literal, mismatch.got)
                except (OSError, FodSourceUpdateError) as source_exc:
                    raise BuildTargetError(f"cannot update fixed-output hash: {source_exc}") from source_exc
                error_console.print(
                    "".join(
                        difflib.unified_diff(
                            source.splitlines(keepends=True),
                            updated.splitlines(keepends=True),
                            fromfile=str(target.file),
                            tofile=str(target.file),
                        )
                    ),
                    markup=False,
                    highlight=False,
                    end="",
                )
                updates += 1
                if dry_run:
                    return {}, updates
                target.file.write_text(updated)
                await session.reset_file_cache()
                continue
        return outputs, updates


def _print_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")
